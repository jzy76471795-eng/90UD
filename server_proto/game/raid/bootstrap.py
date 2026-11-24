"""Warm and pin the current-US raid dependencies before local listeners start."""
from dataclasses import dataclass, replace
import hashlib
import ipaddress
import os
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from .catalog import load_runtime_catalog
from .landing import load_runtime_raid_login_index
from .progression import PvfProgram, BarrierConfiguration
from .rewards import RewardConfiguration
from .inventory_adapter import build_runtime_reward_resolver
from .actual_actor_classifier import ActualActorClassifier, ActorSourceSpec

EXPECTED_EXE = '14b85f7e4325ef2d3c7a420740a88ef16bdd1c8f5bcda97044cc630139a56634'
_LOCK = RLock()
_RUNTIME = None


@dataclass(frozen=True)
class RaidRuntimeData:
    catalog: object
    programs: object
    rewards: object
    landings: object
    source_sha256: str
    reward_resolver: object
    actor_classifier: object
    revive_limits: object = None
    skill_rules: object = None


def validate_local_settings(args, mode, environment=None):
    env = os.environ if environment is None else environment
    for name in ('host', 'advertise_host', 'control_host', 'login_host'):
        try:
            if not ipaddress.ip_address(getattr(args, name)).is_loopback:
                raise ValueError(name)
        except ValueError as exc:
            raise RuntimeError('local raid test requires loopback ' + name) from exc
    if mode != 'core':
        raise RuntimeError('local raid test requires completed core warmup')
    if env.get('USDOF_CHANNELINFO_UNIQUE_PORTS') != '1':
        raise RuntimeError('local raid test requires separate channel TCP ports')
    first = int(env.get('USDOF_CHANNELINFO_FIRST_CHANNEL_ID', '10'))
    count = int(env.get('USDOF_CHANNELINFO_MAX_CHANNELS', '1'))
    if not first <= 23 < first + count or not first <= 32 < first + count:
        raise RuntimeError('local raid test must expose ch23 and ch32')
    profile = env.get('USDOF_GAME_OPCODE1_PROFILE', 'usdof90-channelinfo-protobuf').strip().lower().replace('_', '-')
    if (profile not in {'usdof90-protobuf', 'usdof90-channelinfo', 'usdof90-channelinfo-protobuf', 'us90-protobuf', 'protobuf'}
            or env.get('USDOF_GAME_CHANNELINFO_PLAIN')
            or int(env.get('USDOF_GAME_CHANNELINFO_MODE', '4')) != 4
            or int(env.get('USDOF_GAME_CHANNELINFO_PACKET_TYPE', '1')) != 1):
        raise RuntimeError('local raid requires the verified CHANNELINFO protobuf profile and mode')
    period = env.get('USDOF_RAID_ENTRY_PERIOD_KEY', '').strip()
    if not period or len(period) > 128:
        raise RuntimeError('local raid test requires an explicit entry-period key')
    return period


def validate_public_settings(args, mode, environment=None):
    """Use the already-deployed authenticated public lobby channel profile."""
    env = os.environ if environment is None else environment
    if mode != 'core' or env.get('USDOF_RAID_LOBBY') != '1' or env.get('USDOF_RAID_LOCAL') != '0':
        raise RuntimeError('public raid battle requires the warmed owned lobby, not local-test mode')
    if env.get('USDOF_CHANNELINFO_UNIQUE_PORTS') != '1':
        raise RuntimeError('public raid battle requires separate channel TCP ports')
    count = int(env.get('USDOF_CHANNELINFO_MAX_CHANNELS', '1'))
    if env.get('USDOF_LOCAL_RAID_CHANNELS') != '1' or count < 3:
        raise RuntimeError('public raid battle requires both projected raid channels and an ordinary channel')
    profile = env.get('USDOF_GAME_OPCODE1_PROFILE', 'usdof90-channelinfo-protobuf').strip().lower().replace('_', '-')
    if (profile not in {'usdof90-protobuf', 'usdof90-channelinfo', 'usdof90-channelinfo-protobuf', 'us90-protobuf', 'protobuf'}
            or env.get('USDOF_GAME_CHANNELINFO_PLAIN')
            or int(env.get('USDOF_GAME_CHANNELINFO_MODE', '0')) != 0
            or int(env.get('USDOF_GAME_CHANNELINFO_PACKET_TYPE', '1')) != 1):
        raise RuntimeError('public raid battle requires the deployed CHANNELINFO profile')


def validate_public_listeners():
    from channel.channel_info import collect_game_listen_ports, game_port_for_channel_id
    ports = collect_game_listen_ports()
    if not {game_port_for_channel_id(23), game_port_for_channel_id(32)} <= set(ports):
        raise RuntimeError('effective public channel projection omits a raid listener')
    return ports


def preload_runtime():
    global _RUNTIME
    from data import CLIENT_DIR
    from pvf import pvf_runtime
    with _LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        if not pvf_runtime.is_runtime_ready():
            raise RuntimeError('raid dependencies require the completed core PVF warmup')
        # Prefer the same explicit EXE path used by the existing codec loader.
        exe = Path(os.environ.get('USDOF_CLIENT_EXE') or (Path(CLIENT_DIR) / 'DFO.exe'))
        if hashlib.sha256(exe.read_bytes()).hexdigest() != EXPECTED_EXE:
            raise RuntimeError('raid protocol evidence does not match the current client EXE')
        source_sha = pvf_runtime.source_sha256().lower()
        catalog = load_runtime_catalog()
        failures = [issue for issue in catalog.issues if issue.scope in ('catalog', 'ordinary_dungeon', 'entry_reward')]
        if failures:
            raise RuntimeError('raid resource closure failed: ' + repr(failures))
        barrier = BarrierConfiguration.from_action_texts(
            pvf_runtime.read_text('passiveobject/actionobject/map/anton/phase2/transmission/action/transmission_s_last.act'),
            pvf_runtime.read_text('passiveobject/actionobject/map/anton/phase2/transmission/action/transmission_l_last.act'),
            tuple(pvf_runtime.read_text('monster/newmonsters/anton/phase3/constructor/action/last' + suffix + '.act') for suffix in ('', '_1', '_2', '_3')),
        )
        programs, rewards = {}, {}
        for config in catalog.raids:
            if config.raid_id not in (0, 1) or len(config.phases) != 2 or not config.entry_cost or config.daily_account_enter_limit is None:
                raise RuntimeError('unsupported current-US normal raid configuration')
            family = 'anton' if config.raid_id == 0 else 'luke'
            text = pvf_runtime.read_text(config.path)
            programs[config.raid_id] = PvfProgram.compile(family, text, pvf_runtime.read_text(config.path + '.symbol'), barrier=barrier if family == 'anton' else None)
            rewards[config.raid_id] = RewardConfiguration.parse(family, text)
        if set(programs) != {0, 1}:
            raise RuntimeError('current-US Anton and Luke configurations are required')
        from .luke_mechanics import LukeMechanics
        luke_config = next(config for config in catalog.raids if config.raid_id == 1)
        luke_ids = {d.dungeon_id for phase in luke_config.phases for d in phase.dungeons}
        luke = LukeMechanics.load(pvf_runtime, luke_config, programs[1],
                                  tuple(d for d in catalog.dungeons if d.dungeon_id in luke_ids))
        programs[1] = replace(programs[1], luke=luke)
        # Until premium-card flags have current-client evidence, ordinary
        # kind0 preserves the verified native ordinary card presentation.
        resolver = build_runtime_reward_resolver(os.environ['USDOF_PVF_PATH'], display_kind_resolver=lambda meta, choice: 0)
        for configuration in rewards.values():
            resolver.preflight(configuration)
        specs = (ActorSourceSpec('passiveobject',
                 'passiveobject/actionobject/map/anton/phase2/transmission/transmission_l.obj',
                 'passiveobject/actionobject/map/anton/phase2/transmission/action/transmission_l_last.act',
                 'large_transmission'),) + tuple(
            ActorSourceSpec('monster', 'monster/newmonsters/anton/phase3/constructor/constructor' + suffix + '.mob',
                            'monster/newmonsters/anton/phase3/constructor/action/last' + suffix + '.act',
                            'hatchery_constructor') for suffix in ('', '_1', '_2', '_3'))
        classifier = ActualActorClassifier.warm(pvf_runtime, specs, expected_barrier=barrier,
            small_action_path='passiveobject/actionobject/map/anton/phase2/transmission/action/transmission_s_last.act')
        classifier.bind_terminal_destruction(pvf_runtime,
            'monster/newmonsters/anton/phase2/energycut/egeneu/egeneu.mob')
        from .revive_limits import ReviveLimits
        revive = {c.raid_id: ReviveLimits.load(pvf_runtime, c, catalog.dungeons) for c in catalog.raids}
        from .skills import parse_rules
        skills = {c.raid_id: MappingProxyType(parse_rules(pvf_runtime.read_text(c.path + '.buff')))
                  for c in catalog.raids}
        _RUNTIME = RaidRuntimeData(catalog, MappingProxyType(programs), MappingProxyType(rewards),
                                   MappingProxyType(load_runtime_raid_login_index()), source_sha, resolver, classifier,
                                   MappingProxyType(revive), MappingProxyType(skills))
        return _RUNTIME


def runtime_data():
    if _RUNTIME is None:
        raise RuntimeError('local raid dependencies were not warmed before gameplay')
    return _RUNTIME

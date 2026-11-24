"""Warm current PVF list/script/ACT closure, then classify owned dead rows.

No actor ID defaults exist here. The caller supplies paths from its verified
bootstrap; the native lists supply IDs. A returned source is not a completion
proof: RaidRunBridge must first authenticate the actual room/actor journal.
"""

from dataclasses import dataclass
from hashlib import sha256
from posixpath import dirname, join, normpath
import re
from types import MappingProxyType

from .progression import BarrierConfiguration
from .runtime import RaidError


class ActorClassificationError(RaidError):
    pass


@dataclass(frozen=True)
class ActorSourceSpec:
    prefix: str
    path: str
    last_action_path: str
    source: str


@dataclass(frozen=True)
class ActualActorBinding:
    actor_id: int
    prefix: str
    path: str
    last_action_path: str
    source: str


@dataclass(frozen=True)
class ResourceEvidence:
    path: str
    sha256: str


def _path(value):
    if not isinstance(value, str) or not value.strip():
        raise ActorClassificationError("explicit PVF resource path required")
    raw = value.strip().replace("\\", "/").lower()
    path = normpath(raw)
    if raw.startswith("/") or path in (".", "..") or path.startswith("../") or ":" in path:
        raise ActorClassificationError("PVF path escapes its resource root")
    return path


def _listing(text, prefix):
    tokens = re.findall(r"`[^`]*`|[^\s]+", re.sub(r"(?m)#.*$", "", str(text)))
    if len(tokens) % 2:
        raise ActorClassificationError("incomplete native actor list")
    result = {}
    for i in range(0, len(tokens), 2):
        try:
            code = int(tokens[i])
        except ValueError as exc:
            raise ActorClassificationError("invalid native actor list ID") from exc
        token = tokens[i + 1]
        if not (0 <= code <= 0xffffffff) or code in result or not token.startswith("`") or not token.endswith("`"):
            raise ActorClassificationError("duplicate/invalid native actor list row")
        # Real native lists include reserved IDs whose path is an empty tick
        # string. Preserve the ID for duplicate checks; it can match no spec.
        if not token[1:-1].strip():
            result[code] = ""
            continue
        relative = _path(token[1:-1])
        path = relative if relative.startswith(prefix + "/") else prefix + "/" + relative
        result[code] = path
    return result


def _last_action(script_path, text, prefix):
    refs = re.findall(r"(?is)\[last action\]\s*`([^`]+)`", text)
    if len(refs) != 1:
        raise ActorClassificationError("actor requires exactly one real last action")
    relative = refs[0].strip().replace("\\", "/").lower()
    path = _path(relative if relative.startswith(prefix + "/") else join(dirname(script_path), relative))
    if not path.startswith(prefix + "/") or not path.endswith(".act"):
        raise ActorClassificationError("actor terminal action escaped its native namespace")
    return path


class ActualActorClassifier:
    def __init__(self, bindings, barrier, evidence):
        self.bindings, self.barrier, self.evidence = tuple(bindings), barrier, tuple(evidence)
        self._bindings = MappingProxyType({(b.prefix, b.actor_id): b for b in self.bindings})
        self._terminal_destructions = MappingProxyType({})

    def bind_terminal_destruction(self, source, actor_path):
        """Prewarm the verified boss LAST -> passive DESTROY dependency.

        DESTROY is a local ACT effect, not a second guaranteed C0027 report.
        Only this unconditional frame-zero, exact-index passive action is
        accepted; conditional or multi-behavior scripts need another owner.
        """
        path = _path(actor_path)
        if not path.startswith('monster/') or not path.endswith('.mob'):
            raise ActorClassificationError('terminal owner must be an actual monster')
        listing = source.read_text('monster/monster.lst')
        script = source.read_text(path)
        action_path = _last_action(path, script, 'monster')
        action = source.read_text(action_path)
        triggers = re.findall(r'(?is)\[trigger\](.*?)\[/trigger\]', action)
        behaviors = re.findall(r'(?is)\[behavior\](.*?)\[/behavior\]', action)
        normalize = lambda value: re.sub(r'\s+', ' ', value).strip().lower()
        match = re.fullmatch(
            r'\[frame\] 0 0 \[which\] \[passive\] \[checkup\] '
            r'\[is index\] (\d+) \[/is index\] \[/checkup\] '
            r'\[do behavior\] \[checkup object\] 0',
            normalize(triggers[0]) if len(triggers) == 1 else '')
        if not match or len(behaviors) != 1 or normalize(behaviors[0]) != '[destroy]':
            raise ActorClassificationError('unverified terminal passive destruction')
        target = int(match[1])
        binding = self._bindings.get(('passiveobject', target))
        if binding is None or binding.source != 'large_transmission':
            raise ActorClassificationError('terminal target lacks a verified barrier action')
        owners = [code for code, actual in _listing(listing, 'monster').items() if actual == path]
        if not owners:
            raise ActorClassificationError('terminal owner absent from actual monster list')
        self._terminal_destructions = MappingProxyType({code: (target,) for code in owners})
        self.evidence += tuple(ResourceEvidence(p, sha256(t.encode('utf-8')).hexdigest())
                               for p, t in ((path, script), (action_path, action)))

    def destroyed_passives(self, actual_row):
        if (not isinstance(actual_row, dict) or actual_row.get('killed') is not True
                or actual_row.get('kind') != 'monster' or actual_row.get('type') != 3):
            return ()
        return self._terminal_destructions.get(actual_row.get('code'), ())

    @classmethod
    def warm(cls, source, specs, *, expected_barrier, small_action_path):
        """Pre-start only: exact native lists and last ACTs, no fallback IDs.

        specs order is large, then the four verified hatchery terminal scripts
        in the same order used by the existing actual BarrierConfiguration.
        The returned callable never reads a file or PVF on the combat path.
        """
        specs = tuple(specs)
        if (len(specs) != 5 or not isinstance(expected_barrier, BarrierConfiguration)
                or not isinstance(specs[0], ActorSourceSpec)
                or (specs[0].prefix, specs[0].source) != ("passiveobject", "large_transmission")
                or any(not isinstance(s, ActorSourceSpec) or (s.prefix, s.source) != ("monster", "hatchery_constructor") for s in specs[1:])):
            raise ActorClassificationError("one actual large and four ordered constructor path specs required")
        paths = tuple(_path(s.path) for s in specs)
        actions = tuple(_path(s.last_action_path) for s in specs)
        if len(set(paths)) != 5 or len(set(actions)) != 5:
            raise ActorClassificationError("actor/terminal-action path ownership is ambiguous")
        texts, evidence = {}, []

        def read(path):
            if path not in texts:
                try:
                    value = source.read_text(path)
                except (KeyError, OSError, ValueError) as exc:
                    raise ActorClassificationError("actual PVF resource unavailable: " + path) from exc
                if not isinstance(value, str) or not value.strip():
                    raise ActorClassificationError("actual PVF resource is empty: " + path)
                texts[path] = value
                evidence.append(ResourceEvidence(path, sha256(value.encode("utf-8")).hexdigest()))
            return texts[path]

        lists = {prefix: _listing(read(prefix + "/" + prefix + ".lst"), prefix) for prefix in ("passiveobject", "monster")}
        bindings, action_texts = [], []
        for spec, path, action in zip(specs, paths, actions):
            suffix = ".obj" if spec.prefix == "passiveobject" else ".mob"
            if not path.startswith(spec.prefix + "/") or not path.endswith(suffix):
                raise ActorClassificationError("actor path does not belong to its native namespace")
            actual_ids = tuple(code for code, candidate in lists[spec.prefix].items() if candidate == path)
            if not actual_ids or any(code <= 0 for code in actual_ids):
                raise ActorClassificationError("actual actor path absent from native list: " + path)
            actual_action = _last_action(path, read(path), spec.prefix)
            if actual_action != action:
                raise ActorClassificationError("actor last action differs from the verified bootstrap action: " + path)
            action_texts.append(read(action))
            bindings.extend(ActualActorBinding(code, spec.prefix, path, action, spec.source) for code in actual_ids)
        try:
            barrier = BarrierConfiguration.from_action_texts(read(_path(small_action_path)), action_texts[0], action_texts[1:])
        except RaidError as exc:
            raise ActorClassificationError("actual terminal barrier actions failed compilation") from exc
        if barrier != expected_barrier:
            raise ActorClassificationError("actual action amounts differ from the frozen bootstrap barrier")
        return cls(bindings, barrier, evidence)

    def __call__(self, actual_row):
        """After bridge ownership checks only; this returns a source, not proof."""
        if not isinstance(actual_row, dict) or actual_row.get("killed") is not True:
            return None
        code, actor_type = actual_row.get("code"), actual_row.get("type")
        if type(code) is not int or type(actor_type) is not int or not 1 <= code <= 0xffffffff:
            return None
        kind = actual_row.get("kind")
        if actor_type == 9 and kind in ("passive_object", "special_passive"):
            prefix = "passiveobject"
        elif actor_type in (0, 1, 2, 3) and kind in ("monster", "monster_condition", "conditional_summon_monster"):
            prefix = "monster"
        else:
            return None
        binding = self._bindings.get((prefix, code))
        return binding.source if binding is not None else None

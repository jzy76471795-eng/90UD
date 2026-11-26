using System.Globalization;
using System.Text;
using System.Text.Json;

sealed class Adventure
{
    internal const int MaxReplyBytes = 16384;
    private static readonly string[] Periods = { "daily", "weekly", "season" };
    private JsonElement snapshot;
    private uint token;

    internal static bool IsRequest(Request request) => request.Action switch
    {
        9 => request.Pickup == 0 && request.Mask == 0,
        10 => request.Pickup >> 16 < 3 && (request.Pickup & 0xffff) > 0 && request.Mask > 0,
        11 => request.Pickup is > 0 and <= 80 && request.Mask > 0,
        12 => request.Pickup == 0 && request.Mask > 0,
        _ => false
    };

    internal async Task<byte[]> Run(Request request, Func<object, Task<JsonElement>> post)
    {
        if (request.Action != 9)
        {
            if (request.Mask != token || !snapshot.GetProperty("active").GetBoolean())
                throw new InvalidOperationException("探险记已刷新，请重试");
            var season = snapshot.GetProperty("season");
            var target = FormattableString.Invariant(
                $"{snapshot.GetProperty("adventure_target").GetString()}/{season.GetProperty("activity_start").GetUInt64()}");
            var command = request.Action switch
            {
                10 => Mission(season, request.Pickup, target),
                11 => Reward(season, request.Pickup, target),
                _ => Reward(season, 0, target)
            };
            await post(command);
        }

        var state = await post(new { action = "adventure-status", request_id = RequestId() });
        var nextToken = snapshot.ValueKind != JsonValueKind.Undefined && snapshot.GetRawText() == state.GetRawText()
            ? token : token == uint.MaxValue ? 1 : token + 1;
        byte[] wire = Encode(state, nextToken, DateTimeOffset.UtcNow.ToUnixTimeSeconds());
        snapshot = state;
        token = nextToken;
        return wire;
    }

    private static object Mission(JsonElement season, uint selection, string target)
    {
        string period = Periods[selection >> 16];
        uint id = selection & 0xffff;
        foreach (var row in season.GetProperty("missions").GetProperty(period).EnumerateArray())
        {
            if (row.GetProperty("mission_id").GetUInt32() == id && row.GetProperty("can_complete").GetBoolean())
            {
                target = FormattableString.Invariant($"{target}/{period}/{row.GetProperty("bucket").GetUInt32()}/{id}");
                return new { action = "adventure-complete", target, request_id = RequestId() };
            }
        }
        throw new InvalidOperationException("当前任务无法完成");
    }

    private static object Reward(JsonElement season, uint level, string target)
    {
        foreach (var reward in season.GetProperty("basic_rewards").EnumerateArray())
        {
            if (!reward.GetProperty("can_claim").GetBoolean()) continue;
            if (level == 0)
                return new { action = "adventure-claim-all", target, request_id = RequestId() };
            if (reward.GetProperty("level").GetUInt32() == level)
            {
                target += "/" + level.ToString(CultureInfo.InvariantCulture);
                return new { action = "adventure-claim", target, request_id = RequestId() };
            }
        }
        throw new InvalidOperationException("当前没有可领取的奖励");
    }

    private static string RequestId() => Guid.NewGuid().ToString("N");

    private static uint Flag(JsonElement row, string key, int bit) =>
        row.GetProperty(key).GetBoolean() ? 1u << bit : 0;

    internal static byte[] Encode(JsonElement state, uint token, long now)
    {
        using var bytes = new MemoryStream();
        using var wire = new BinaryWriter(bytes, Encoding.UTF8, true);
        bool active = state.GetProperty("active").GetBoolean();
        wire.Write(2u);
        wire.Write(active ? 1u : 0u);
        if (!active) return bytes.ToArray();
        var season = state.GetProperty("season");
        uint level = season.GetProperty("level").GetUInt32();
        var nextXp = season.GetProperty("xp_for_next_level");
        long seconds = Math.Max(0, season.GetProperty("activity_end").GetInt64() - now);
        if (level is 0 or > 80 || seconds > 100 * 86400L)
            throw new InvalidDataException("无效的探险记赛季状态");
        wire.Write(token);
        wire.Write(level);
        wire.Write(season.GetProperty("level_xp").GetUInt32());
        wire.Write(nextXp.ValueKind == JsonValueKind.Null ? 0u : nextXp.GetUInt32());
        wire.Write((uint)((seconds + 86399L) / 86400L));
        var missions = season.GetProperty("missions");
        foreach (var period in Periods)
        {
            var rows = missions.GetProperty(period);
            int count = rows.GetArrayLength();
            if (count > 32) throw new InvalidDataException("探险记任务超出支持范围");
            wire.Write((uint)count);
            foreach (var row in rows.EnumerateArray())
            {
                byte[] name = Encoding.UTF8.GetBytes(row.GetProperty("name").GetString() ?? "");
                if (name.Length is 0 or > 256)
                    throw new InvalidDataException("无效的探险记任务名称");
                wire.Write(row.GetProperty("mission_id").GetUInt32());
                wire.Write(row.GetProperty("current").GetUInt32());
                wire.Write(row.GetProperty("target").GetUInt32());
                wire.Write(row.GetProperty("xp").GetUInt32());
                wire.Write(Flag(row, "advanced", 0) | Flag(row, "wired", 1)
                    | Flag(row, "claimed", 2) | Flag(row, "can_complete", 3));
                wire.Write((uint)name.Length);
                wire.Write(name);
            }
        }
        var rewards = season.GetProperty("basic_rewards");
        if (rewards.GetArrayLength() > 80) throw new InvalidDataException("探险记奖励超出支持范围");
        wire.Write((uint)rewards.GetArrayLength());
        foreach (var reward in rewards.EnumerateArray())
        {
            wire.Write(reward.GetProperty("level").GetUInt32());
            wire.Write(Flag(reward, "claimed", 0) | Flag(reward, "can_claim", 1));
            var items = reward.GetProperty("items");
            if (items.GetArrayLength() is < 1 or > 4)
                throw new InvalidDataException("探险记奖励格数无效");
            wire.Write((uint)items.GetArrayLength());
            foreach (var item in items.EnumerateArray())
            {
                if (item.GetArrayLength() != 2)
                    throw new InvalidDataException("探险记奖励物品无效");
                wire.Write(item[0].GetUInt32());
                wire.Write(item[1].GetUInt32());
            }
        }
        if (bytes.Length > MaxReplyBytes - 12)
            throw new InvalidDataException("探险记状态超出支持范围");
        return bytes.ToArray();
    }
}

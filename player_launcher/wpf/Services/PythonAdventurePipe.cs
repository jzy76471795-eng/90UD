using System.Buffers.Binary;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace DNFLogin.Services;

internal static class PythonAdventurePipe
{
    private const uint Magic = 0x31564441;
    private const uint State = 1;
    private const uint Buy = 2;
    private const uint UseCapsule = 3;
    private const uint CollectionList = 4;
    private const uint CollectionSet = 5;
    private const uint CollectionRemove = 6;
    private const uint ExpeditionStart = 7;
    private const uint ExpeditionCancel = 8;
    private const uint ExpeditionClaim = 9;
    private const uint WeeklyStatus = 10;
    private const uint WeeklySign = 11;
    private const uint WeeklyMakeUp = 12;
    private const uint WeeklyClaim = 13;
    private const int WeeklyStatusSize = 10;
    private const int CollectionRowSize = 80;
    private const int ExpeditionAreaCount = 4;
    private const int ExpeditionRunSize = 48;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint processId);

    internal static async Task RunAsync(string name, Process game, PythonSession session)
    {
        using var api = PythonClientLauncher.CreateApi();
        using var lifetime = new CancellationTokenSource();
        void Stop(object? sender, EventArgs args) => lifetime.Cancel();
        game.Exited += Stop;
        try
        {
            game.EnableRaisingEvents = true;
            while (!game.HasExited && !lifetime.IsCancellationRequested)
            {
                using var pipe = new NamedPipeServerStream(name, PipeDirection.InOut, 1,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.WaitForConnectionAsync(lifetime.Token).ConfigureAwait(false);
                if (!GetNamedPipeClientProcessId(pipe.SafePipeHandle, out var peer) || peer != game.Id || game.HasExited)
                    continue;
                try
                {
                    using var io = CancellationTokenSource.CreateLinkedTokenSource(lifetime.Token);
                    io.CancelAfter(TimeSpan.FromSeconds(15));
                    await ProcessAsync(pipe, api, session, io.Token).ConfigureAwait(false);
                }
                catch (IOException) { }
                catch (OperationCanceledException) when (!lifetime.IsCancellationRequested) { }
            }
        }
        catch (OperationCanceledException) when (lifetime.IsCancellationRequested) { }
        catch (IOException) { }
        finally
        {
            game.Exited -= Stop;
        }
    }

    private static async Task ProcessAsync(NamedPipeServerStream pipe, PythonLauncherApi api, PythonSession session, CancellationToken cancellation)
    {
        var request = new byte[28];
        if (!await ReadRequestAsync(pipe, request, cancellation).ConfigureAwait(false))
            return;

        uint Read(int index) => BinaryPrimitives.ReadUInt32LittleEndian(request.AsSpan(index * 4, 4));
        var command = Read(1);
        var nonce = Read(2);
        if (Read(0) != Magic || nonce == 0 || command < State || command > WeeklyClaim)
            return;
        var party = Array.Empty<uint>();
        if (command == ExpeditionStart)
        {
            var count = Read(6);
            if (count < 1 || count > ExpeditionAreaCount)
                return;
            var slots = new byte[ExpeditionAreaCount * sizeof(uint)];
            if (!await ReadRequestAsync(pipe, slots, cancellation).ConfigureAwait(false))
                return;
            party = new uint[count];
            for (int slot = 0; slot < party.Length; ++slot)
                party[slot] = BinaryPrimitives.ReadUInt32LittleEndian(slots.AsSpan(slot * sizeof(uint)));
        }

        var reply = new byte[20];
        byte[]? rows = null;
        byte[]? expedition = null;
        byte[]? expeditionState = null;
        byte[]? weekly = null;
        void Write(int index, uint value) => BinaryPrimitives.WriteUInt32LittleEndian(reply.AsSpan(index * 4, 4), value);
        Write(0, Magic);
        try
        {
            if (command >= WeeklyStatus)
            {
                if (Read(6) != 0 || (command == WeeklyStatus && (Read(3) != 0 || Read(4) != 0))
                    || (command is WeeklyStatus or WeeklyClaim ? Read(5) != 0 : Read(5) is < 1 or > 10))
                    return;
                object body = command switch
                {
                    WeeklyStatus => new { action = "weekly_status", request_id = nonce },
                    WeeklyClaim => new { action = "weekly_claim", request_id = nonce, character_id = Read(3),
                                         connection_generation = Read(4) },
                    _ => new { action = command == WeeklySign ? "weekly_sign" : "weekly_make_up",
                               request_id = nonce, character_id = Read(3), connection_generation = Read(4), week = Read(5) },
                };
                var response = await api.AdventureOperationAsync(session, body).ConfigureAwait(false);
                var status = response.GetProperty("attendance");
                if (status.GetArrayLength() != WeeklyStatusSize)
                    throw new InvalidOperationException("签到状态长度无效");
                weekly = status.EnumerateArray().Select(value => value.GetByte()).ToArray();
                Write(2, response.GetProperty("character_id").GetUInt32());
                Write(3, response.GetProperty("connection_generation").GetUInt32());
                Write(4, nonce);
            }
            else if (command >= CollectionList)
            {
                object body = command switch
                {
                    CollectionList => new { action = "list" },
                    CollectionSet => new { action = "set", character_id = Read(5) },
                    CollectionRemove => new { action = "remove", character_id = Read(5) },
                    ExpeditionStart => new { action = "expedition_start", request_id = nonce,
                                              area_id = Read(3), rotation_key = Read(4), hours = Read(5), members = party },
                    _ => new { action = command == ExpeditionCancel ? "expedition_cancel" : "expedition_claim",
                               run_id = (ulong)Read(3) | (ulong)Read(4) << 32 },
                };
                var response = await api.AdventureCollectionAsync(session, body).ConfigureAwait(false);
                rows = CollectionRows(response);
                expedition = ExpeditionAttributes(response);
                expeditionState = ExpeditionState(response);
                Write(2, (uint)(expedition?.Length ?? 0));
                Write(3, (uint)(expeditionState?.Length ?? 0));
                Write(4, (uint)(rows.Length / CollectionRowSize));
            }
            else
            {
                object body = command switch
                {
                    State => new { action = "state", request_id = nonce },
                    Buy => new { action = "buy", request_id = nonce, character_id = Read(3),
                                 connection_generation = Read(4), item_id = Read(5), quantity = Read(6) },
                    _ => new { action = "use_capsule", request_id = nonce, character_id = Read(3),
                               connection_generation = Read(4), quantity = Read(6) },
                };
                var response = await api.AdventureOperationAsync(session, body).ConfigureAwait(false);
                Write(2, response.GetProperty("character_id").GetUInt32());
                Write(3, response.GetProperty("connection_generation").GetUInt32());
                if (response.TryGetProperty("character_level", out var level))
                    Write(4, level.GetUInt32());
            }
        }
        catch (Exception error) when (error is InvalidOperationException or HttpRequestException or TaskCanceledException
                                      or KeyNotFoundException or System.Text.Json.JsonException or FormatException)
        {
            Write(1, 1);
            rows = null;
            expedition = null;
            expeditionState = null;
            weekly = null;
        }
        await pipe.WriteAsync(reply, cancellation).ConfigureAwait(false);
        if (rows is not null && rows.Length != 0)
            await pipe.WriteAsync(rows, cancellation).ConfigureAwait(false);
        if (expedition is not null)
            await pipe.WriteAsync(expedition, cancellation).ConfigureAwait(false);
        if (expeditionState is not null)
            await pipe.WriteAsync(expeditionState, cancellation).ConfigureAwait(false);
        if (weekly is not null)
            await pipe.WriteAsync(weekly, cancellation).ConfigureAwait(false);
    }

    private static async Task<bool> ReadRequestAsync(Stream pipe, byte[] buffer, CancellationToken cancellation)
    {
        for (int used = 0; used < buffer.Length;)
        {
            var count = await pipe.ReadAsync(buffer.AsMemory(used), cancellation).ConfigureAwait(false);
            if (count == 0)
                return false;
            used += count;
        }
        return true;
    }

    private static byte[]? ExpeditionState(System.Text.Json.JsonElement response)
    {
        if (!response.TryGetProperty("expedition", out var status))
            return null;
        const int header = 16;
        var result = new byte[header + ExpeditionAreaCount * ExpeditionRunSize];
        void Write(int offset, uint value) => BinaryPrimitives.WriteUInt32LittleEndian(result.AsSpan(offset), value);
        void WriteWide(int offset, ulong value) => BinaryPrimitives.WriteUInt64LittleEndian(result.AsSpan(offset), value);
        WriteWide(0, status.GetProperty("server_time").GetUInt64());
        Write(8, status.GetProperty("rotation_key").GetUInt32());
        if (response.TryGetProperty("expedition_operation", out var operation)
            && operation.ValueKind == System.Text.Json.JsonValueKind.Object)
            Write(12, operation.GetProperty("glory_credited").GetUInt32());
        foreach (var run in status.GetProperty("runs").EnumerateArray())
        {
            int area = run.GetProperty("area_id").GetInt32();
            if (area < 1 || area > ExpeditionAreaCount)
                throw new InvalidOperationException("远征区域无效");
            int offset = header + (area - 1) * ExpeditionRunSize;
            WriteWide(offset, run.GetProperty("run_id").GetUInt64());
            WriteWide(offset + 8, run.GetProperty("start_unix").GetUInt64());
            WriteWide(offset + 16, run.GetProperty("finish_unix").GetUInt64());
            Write(offset + 24, run.GetProperty("glory_reward").GetUInt32());
            var members = run.GetProperty("members");
            var attributes = run.GetProperty("required_attributes");
            if (members.GetArrayLength() > ExpeditionAreaCount || attributes.GetArrayLength() != area)
                throw new InvalidOperationException("远征队员或属性无效");
            int field = offset + 28;
            foreach (var member in members.EnumerateArray())
            {
                Write(field, member.GetUInt32());
                field += sizeof(uint);
            }
            field = offset + 44;
            foreach (var attribute in attributes.EnumerateArray())
                result[field++] = attribute.GetByte();
        }
        return result;
    }

    private static byte[]? ExpeditionAttributes(System.Text.Json.JsonElement response)
    {
        if (!response.TryGetProperty("expedition_attributes", out var areas))
            return null;
        var result = new byte[ExpeditionAreaCount * ExpeditionAreaCount];
        for (int area = 1; area <= ExpeditionAreaCount; ++area)
        {
            var attributes = areas.GetProperty(area.ToString(System.Globalization.CultureInfo.InvariantCulture));
            if (attributes.GetArrayLength() != area)
                throw new InvalidOperationException("远征区域属性数量无效");
            int offset = (area - 1) * ExpeditionAreaCount;
            foreach (var attribute in attributes.EnumerateArray())
                result[offset++] = attribute.GetByte();
        }
        return result;
    }

    private static byte[] CollectionRows(System.Text.Json.JsonElement response)
    {
        var characters = response.GetProperty("characters");
        int count = characters.GetArrayLength();
        if (count > 128)
            throw new InvalidOperationException("收藏角色数量超出客户端容量");
        var rows = new byte[count * CollectionRowSize];
        var index = 0;
        foreach (var entry in characters.EnumerateArray())
        {
            int offset = index++ * CollectionRowSize;
            void Write(int field, uint value) =>
                BinaryPrimitives.WriteUInt32LittleEndian(rows.AsSpan(offset + field * 4, 4), value);
            Write(0, entry.GetProperty("character_id").GetUInt32());
            Write(1, entry.GetProperty("job").GetUInt32());
            Write(2, entry.GetProperty("branch").GetUInt32());
            Write(3, entry.GetProperty("expertise_level").GetUInt32());
            Write(4, entry.GetProperty("registered").GetBoolean() ? 1u : 0u);
            var name = entry.GetProperty("name").GetString() ?? "";
            Encoding.Unicode.GetEncoder().Convert(name.AsSpan(), rows.AsSpan(offset + 20, 46),
                true, out _, out _, out _);
            var attributes = entry.GetProperty("attribute_ids");
            if (attributes.GetArrayLength() > 3)
                throw new InvalidOperationException("收藏角色远征属性超过三个");
            int attributeOffset = offset + 68;
            foreach (var attribute in attributes.EnumerateArray())
                rows[attributeOffset++] = attribute.GetByte();
            if (entry.TryGetProperty("expertise_exp", out var expertise))
                BinaryPrimitives.WriteUInt64LittleEndian(rows.AsSpan(offset + 72, 8), expertise.GetUInt64());
        }
        return rows;
    }
}

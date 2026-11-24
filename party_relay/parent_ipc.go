package main

// In the launcher-owned IPC mode the DLL continues pinning one OS process
// (the launcher) while the authenticated network worker can be replaced. Native
// snapshots and replies travel only over its inherited private stdin/stdout.
import (
	"bufio"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"io"
	"os"
)

type parentExchange struct {
	Sequence uint64 `json:"sequence"`
	Frame    string `json:"native_frame"`
}

func parentReply(request parentExchange, game gameIdentity, exchange func(nativeReport) localView) (parentExchange, error) {
	raw, err := base64.StdEncoding.DecodeString(request.Frame)
	if err != nil || request.Sequence == 0 || len(raw) < ipcHeader || len(raw) > ipcHeader+200 {
		return parentExchange{}, errors.New("bounded parent IPC frame required")
	}
	sequence := binary.LittleEndian.Uint64(raw[16:])
	size, err := ipcSize(raw[:ipcHeader], 1, sequence)
	if err != nil || size != len(raw)-ipcHeader {
		return parentExchange{}, errors.New("parent IPC frame size")
	}
	native, err := parseNativeSnapshot(raw[ipcHeader:], game)
	if err != nil {
		return parentExchange{}, err
	}
	view := exchange(native)
	view.Game = game
	payload, err := encodeLocalView(view)
	if err != nil {
		return parentExchange{}, err
	}
	frame, err := ipcFrame(2, sequence, payload)
	return parentExchange{Sequence: request.Sequence, Frame: base64.StdEncoding.EncodeToString(frame)}, err
}

func runParentEdge(parent context.Context, cfg edgeStartup, reader *bufio.Reader, readyPath string) error {
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	edge, err := dialEdge(ctx, cfg)
	if err != nil {
		return err
	}
	defer edge.close()
	failures := make(chan error, 8)
	run := func(f func() error) {
		go func() {
			if err := f(); err != nil {
				select {
				case failures <- err:
				case <-ctx.Done():
				}
			}
		}()
	}
	run(func() error { return edge.writer(ctx) })
	run(func() error { return edge.reader(ctx) })
	run(func() error { return edge.poll(ctx, failures) })
	run(func() error {
		var game gameIdentity
		if err := readPrivateLine(reader, &game); err != nil {
			return err
		}
		created, err := processCreated(game.PID)
		if err != nil || game.PID == 0 || created != game.Created {
			return errors.New("parent game process binding")
		}
		encoder := json.NewEncoder(os.Stdout)
		var sequence uint64
		for ctx.Err() == nil {
			var request parentExchange
			if err := readPrivateLine(reader, &request); err != nil {
				return err
			}
			sequence++
			if request.Sequence != sequence || sequence == 0 {
				return errors.New("parent IPC exchange replay")
			}
			created, err = processCreated(game.PID)
			if err != nil || created != game.Created {
				return errors.New("parent game exited")
			}
			reply, err := parentReply(request, game, edge.nativeExchange)
			if err != nil {
				return err
			}
			if err = encoder.Encode(reply); err != nil {
				return err
			}
		}
		return io.EOF
	})
	if readyPath != "" {
		b, _ := json.Marshal(map[string]any{"pid": os.Getpid(), "role": "edge", "member_id": cfg.Grant.Member, "native_pipe_ready": true, "parent_native_ipc": true})
		if err = os.WriteFile(readyPath, b, 0600); err != nil {
			return errors.New("edge readiness unavailable")
		}
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-failures:
		return err
	}
}

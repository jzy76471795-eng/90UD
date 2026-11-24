package main

import (
	"context"
	"errors"
	"io"
	"testing"
)

func TestEdgeExitDiagnosticsNeverExposeArbitraryErrorText(t *testing.T) {
	cases := []struct {
		err  error
		code string
	}{
		{nil, "completed"}, {context.Canceled, "cancelled"}, {io.EOF, "private_channel_eof"},
		{errors.New("edge transport closed"), "transport_closed"},
		{errors.New("parent game exited"), "game_exited"},
		{errors.New("https://private.example/?token=secret"), "unclassified"},
	}
	for _, v := range cases {
		if got := edgeExitCode(v.err); got != v.code {
			t.Fatalf("unexpected diagnostic code %q", got)
		}
	}
}

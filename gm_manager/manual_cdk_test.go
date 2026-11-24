package main

import "testing"

func TestManualCDK(t *testing.T) {
	for _, v := range []string{"单/3330/7/10/500/2", "多/3330/1/3339/2/0/0/1", "纯点券/300/0/1"} {
		if _, e := parseManualCDK(v); e != nil {
			t.Fatal(e)
		}
	}
	for _, v := range []string{"缺数量/3330/10/0/1", "空/0/0/1", "错/-1/3/0/0/1", "多/1/10000/0/0/1", "多/1/1/0/0/0"} {
		if _, e := parseManualCDK(v); e == nil {
			t.Fatal(v)
		}
	}
}

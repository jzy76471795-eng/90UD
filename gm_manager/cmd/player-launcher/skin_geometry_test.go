//go:build windows

package main

import (
	"image"
	"testing"
)

func TestOriginalTemplateGeometry(t *testing.T) {
	if got := primaryCard(false); got != image.Rect(892, 273, 1240, 631) {
		t.Fatal("unlogged card drift", got)
	}
	if got := primaryCard(true); got != image.Rect(892, 261, 1240, 643) {
		t.Fatal("logged card drift", got)
	}
	if navBounds[0] != image.Rect(468, 18, 588, 58) || navBounds[2].Max.X != 812 {
		t.Fatal("navigation drift")
	}
	if secondaryTop(false) != 352 || secondaryTop(true) != 340 {
		t.Fatal("secondary vertical center drift")
	}
	if cdkCodeBounds != image.Rect(360, 376, 920, 408) {
		t.Fatal("CDK layout drift")
	}
}

func TestOriginalHistoryUsesOneLinePerRedemption(t *testing.T) {
	got := redemptionHistory([]byte(`{"redemptions":[{"created_at":"today","name":"gift","code":"ABC","character_id":9}]}`))
	if got != "today  gift  ABC\r\n" {
		t.Fatal("original history format drift", got)
	}
}

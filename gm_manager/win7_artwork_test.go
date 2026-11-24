package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestWin7PublishedPlayerIncludesTemplateImages(t *testing.T) {
	root := os.Getenv("GM_PROTECTED_TEMPLATE_BASE")
	if root == "" {
		t.Skip("requires staged protected template")
	}
	template, err := LoadCurrentLauncherTemplate(root)
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := os.ReadFile(filepath.Join(filepath.Dir(template.Path), "Image", "清单.json"))
	if err != nil {
		t.Fatal(err)
	}
	var entries []launcherImageEntry
	if err = json.Unmarshal(manifest, &entries); err != nil || len(entries) == 0 {
		t.Fatal("empty or unreadable artwork manifest", err)
	}
	options := fixturePackage(t)
	options.TemplatePath, options.TemplateSHA256 = template.Path, template.SHA256
	result, err := BuildLauncherPackage(options)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		sum, err := hashLauncherFile(filepath.Join(result.Directory, "Image", entry.Name))
		if err != nil || sum != entry.SHA256 {
			t.Fatal("missing or changed published artwork:", entry.Name, err)
		}
	}
}

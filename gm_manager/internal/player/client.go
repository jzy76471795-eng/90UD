package player

import (
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

//go:embed assets/client-pins.json
var clientPins []byte

//go:embed assets/third-party-notices.txt
var ThirdPartyNotices string

type pinVariant struct {
	Bytes  int64  `json:"bytes"`
	SHA256 string `json:"sha256"`
}
type componentPin struct {
	Path     string       `json:"relative_path"`
	Bytes    int64        `json:"bytes"`
	SHA256   string       `json:"sha256"`
	Variants []pinVariant `json:"approved_variants"`
}
type ClientPlan struct {
	Root, Exe, Pvf string
	DLLs           []string
}

func ValidateComponents(root string, patches PatchManifest) ([]string, error) {
	if e := NoLinks(root); e != nil {
		return nil, e
	}
	if e := patches.Validate(); e != nil {
		return nil, e
	}
	var pins struct {
		DLLs     []componentPin `json:"dlls"`
		Optional []componentPin `json:"optional_dlls"`
	}
	if json.Unmarshal(clientPins, &pins) != nil || len(pins.DLLs) != 16 {
		return nil, errors.New("内置游戏组件清单无效")
	}
	paths := []string{}
	seen := map[string]bool{}
	add := func(p componentPin, optional bool) error {
		path, e := Within(root, p.Path)
		if e != nil {
			return e
		}
		s, e := os.Stat(path)
		if os.IsNotExist(e) && optional {
			return nil
		}
		if e != nil || !s.Mode().IsRegular() {
			return fmt.Errorf("缺少游戏组件：%s", filepath.Base(path))
		}
		h, e := FileHash(path)
		if e != nil {
			return e
		}
		approved := s.Size() == p.Bytes && h == p.SHA256
		var replacement *Patch
		for i := range patches.Items {
			v := &patches.Items[i]
			if strings.EqualFold(p.Path, "us_extend_dll/"+v.FileName) {
				replacement = v
				break
			}
		}
		if replacement != nil {
			approved = s.Size() == replacement.Bytes && h == replacement.SHA256
		} else {
			for _, v := range p.Variants {
				approved = approved || (s.Size() == v.Bytes && h == v.SHA256)
			}
		}
		if !approved {
			return fmt.Errorf("游戏组件版本不匹配：%s", filepath.Base(path))
		}
		lower := strings.ToLower(path)
		if seen[lower] {
			return errors.New("游戏组件路径重复")
		}
		seen[lower] = true
		if !strings.EqualFold(filepath.Base(path), "forbidden-func.dll") {
			paths = append(paths, path)
		}
		return nil
	}
	for _, p := range pins.DLLs {
		if e := add(p, false); e != nil {
			return nil, e
		}
	}
	for _, p := range pins.Optional {
		if e := add(p, true); e != nil {
			return nil, e
		}
	}
	for _, p := range patches.Items {
		path, e := Within(root, "us_extend_dll/"+p.FileName)
		if e != nil {
			return nil, e
		}
		if !seen[strings.ToLower(path)] {
			if e = add(componentPin{Path: "us_extend_dll/" + p.FileName, Bytes: p.Bytes, SHA256: p.SHA256}, false); e != nil {
				return nil, e
			}
		}
	}
	rows, e := os.ReadDir(filepath.Join(root, "us_extend_dll"))
	if e != nil {
		return nil, e
	}
	for _, v := range rows {
		if strings.EqualFold(filepath.Ext(v.Name()), ".dll") && !seen[strings.ToLower(filepath.Join(root, "us_extend_dll", v.Name()))] {
			return nil, fmt.Errorf("存在未经配套的游戏DLL：%s", v.Name())
		}
	}
	return paths, nil
}
func ValidateClient(root string, stage Stage) (ClientPlan, error) {
	root, e := filepath.Abs(root)
	p := ClientPlan{Root: root, Exe: filepath.Join(root, "DFO.exe"), Pvf: filepath.Join(root, "Script.pvf")}
	if e != nil {
		return p, e
	}
	if e = stage.Validate(); e != nil {
		return p, e
	}
	for _, v := range []struct{ Path, Hash string }{{p.Exe, stage.ExeSHA256}, {p.Pvf, stage.PvfSHA256}} {
		if e = NoLinks(v.Path); e != nil {
			return p, e
		}
		h, e := FileHash(v.Path)
		if e != nil || h != v.Hash {
			return p, fmt.Errorf("%s与当前服务器版本不匹配", filepath.Base(v.Path))
		}
	}
	p.DLLs, e = ValidateComponents(root, stage.Patches)
	return p, e
}

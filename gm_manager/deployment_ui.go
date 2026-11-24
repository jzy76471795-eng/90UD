//go:build windows

package main

import (
	"archive/zip"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"

	"github.com/lxn/walk"
	decl "github.com/lxn/walk/declarative"
)

type deploymentUI struct {
	output *walk.TextEdit
	button *walk.PushButton
	source *walk.LineEdit
}

func deploymentPage() decl.TabPage {
	d := &deploymentUI{}
	exe, _ := os.Executable()
	source := filepath.Join(filepath.Dir(exe), "gmserver.exe")
	return decl.TabPage{Title: "一键部署", Layout: decl.VBox{Margins: decl.Margins{12, 12, 12, 12}, Spacing: 10}, Children: []decl.Widget{decl.Label{Text: "GM管理使用HTTPS；一键部署使用本机加密WinRM运维工具。先下载本机备份并校验，再替换GM程序并检查TLS与实际进程，不改游戏源码。"},
		decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.Label{Text: "候选GM程序："}, decl.LineEdit{AssignTo: &d.source, Text: source, StretchFactor: 1}, decl.PushButton{Text: "选择EXE", OnClicked: func() {
			dialog := &walk.FileDialog{Title: "选择候选gmserver.exe", Filter: "EXE (*.exe)|*.exe", FilePath: d.source.Text()}
			if ok, _ := dialog.ShowOpen(applicationWindow); ok {
				d.source.SetText(dialog.FilePath)
			}
		}}}},
		decl.Composite{Layout: decl.HBox{MarginsZero: true, Spacing: 8}, Children: []decl.Widget{decl.PushButton{Text: "下载本机完整备份", OnClicked: d.backup}, decl.PushButton{Text: "更新独立GM服务", OnClicked: d.deploy}, decl.PushButton{Text: "查询原部署回执", OnClicked: d.receipt}, decl.HSpacer{}}}, decl.PushButton{AssignTo: &d.button, Text: "生成本机Go管理服务待部署包", OnClicked: d.prepare}, decl.GroupBox{Title: "准备结果", StretchFactor: 1, Layout: decl.VBox{Margins: decl.Margins{8, 8, 8, 8}}, Children: []decl.Widget{decl.TextEdit{AssignTo: &d.output, ReadOnly: true, VScroll: true, Text: "包内包含Go管理服务和无令牌配置样例；不包含正式数据库、游戏资源、Windows密码或GM令牌。生成后仍需部署审查与实际运行验证。"}}}}}
}
func (d *deploymentUI) prepare() {
	exe, err := os.Executable()
	if err != nil {
		d.output.SetText(err.Error())
		return
	}
	base := filepath.Dir(exe)
	d.button.SetEnabled(false)
	d.output.SetText("正在准备本机待部署包…")
	go func() {
		path, err := prepareManagerPackage(base)
		applicationWindow.Synchronize(func() {
			d.button.SetEnabled(true)
			if err != nil {
				d.output.SetText(err.Error())
				return
			}
			d.output.SetText("本机待部署包已生成：\r\n" + path + "\r\n\r\n本次没有连接、上传或更新服务器。\r\n未来部署前还需核对游戏代码、数据库结构、资源版本、启动配置及本机备份。")
			logUIAction("一键部署", "生成本机待部署包", path, "仅本机打包，未部署")
		})
	}()
}
func prepareManagerPackage(base string) (string, error) {
	return prepareManagerPackageAt(base, filepath.Join(localDataDirectory(), "待部署包"))
}
func prepareManagerPackageAt(base, directory string) (string, error) {
	source := filepath.Join(base, "gmserver.exe")
	if _, err := os.Stat(source); err != nil {
		source = filepath.Join(base, "Go管理服务.exe")
	}
	sum, err := sha256File(source)
	if err != nil {
		return "", fmt.Errorf("缺少已构建的Go管理服务")
	}
	if err = os.MkdirAll(directory, 0700); err != nil {
		return "", err
	}
	file, err := os.CreateTemp(directory, "Go管理服务_"+time.Now().In(chinaTime).Format("20060102_150405")+"_*.zip")
	if err != nil {
		return "", err
	}
	path := file.Name()
	z := zip.NewWriter(file)
	add := func(name string, data []byte) error {
		entry, err := z.Create(name)
		if err != nil {
			return err
		}
		_, err = entry.Write(data)
		return err
	}
	input, err := os.Open(source)
	if err == nil {
		var entry io.Writer
		entry, err = z.Create("Go管理服务.exe")
		if err == nil {
			_, err = io.Copy(entry, input)
		}
		input.Close()
	}
	if err == nil {
		example, _ := json.MarshalIndent(map[string]any{"listen": "127.0.0.1:17890", "token": "", "data_directory": "请使用服务器上运行所需的绝对目录", "server_root": "", "game_database": "", "control_endpoint_file": "", "tls_certificate": "", "tls_key": ""}, "", "  ")
		err = add("配置样例_不能直接启动.json", example)
	}
	if err == nil {
		manifest, _ := json.MarshalIndent(map[string]any{"created_at": time.Now().In(chinaTime), "files": map[string]string{"Go管理服务.exe": sum}, "deployed": false, "database_included": false, "credentials_included": false, "game_resources_included": false}, "", "  ")
		err = add("清单.json", manifest)
	}
	if err == nil {
		err = add("先读.txt", []byte("仅为Go管理服务候选包，未部署。\r\n目标机先运行“Go管理服务.exe -init -config <运行配置绝对路径>”生成独立令牌。\r\n远程监听必须配置TLS证书与私钥，客户端核对证书。\r\n配置实际游戏数据库与控制接口前，按本地备份政策完整核对现有代码、资源、数据库和启动传递。\r\n不覆盖数据库，不自动迁移，不附带玩家密钥或GM令牌。\r\n游戏业务仍逐项迁移，详细完成范围以同版本本机验证与交接为准。"))
	}
	closeZip := z.Close()
	if err == nil {
		err = closeZip
	}
	if err == nil {
		err = file.Sync()
	}
	closeFile := file.Close()
	if err == nil {
		err = closeFile
	}
	if err != nil {
		return "", err
	}
	return path, nil
}

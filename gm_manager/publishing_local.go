//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"newadmin/internal/adminapi"
	"newadmin/internal/downloader"
	"newadmin/internal/launcherbundle"
)

func selectedOutputDirectory(value string) (string, error) {
	return checkedOutputDirectory(value)
}
func launcherOutputPath(parent string, _ string) (string, error) {
	if strings.TrimSpace(parent) == "" {
		return "", fmt.Errorf("请选择输出目录")
	}
	return filepath.Abs(parent)
}
func (p *publishingUI) buildPVF() {
	if p.pvfBusy {
		return
	}
	source, key := p.pvfSource.Text(), p.pvfKey.Text()
	root, err := selectedOutputDirectory(p.pvfDirectory.Text())
	if err != nil {
		p.pvfOutput.SetText(err.Error())
		return
	}
	p.pvfBusy = true
	p.pvfOutput.SetText("正在本机换钥并校验，请等待…")
	go func() {
		directory, err := os.MkdirTemp(root, "PVF_"+time.Now().In(chinaTime).Format("20060102_150405")+"_")
		var result *PVFResult
		newKey := ""
		if err == nil {
			newKey, err = newRandomKeySet()
		}
		if err == nil {
			parameters, _ := json.MarshalIndent(map[string]any{"new_keyset": newKey, "source": source, "server_published": false}, "", "  ")
			err = os.WriteFile(filepath.Join(directory, "生成参数_含密钥.json"), parameters, 0600)
		}
		if err == nil {
			result, err = protectPVFFileDetailed(context.Background(), source, filepath.Join(directory, "Script.pvf"), key, newKey, nil)
		}
		if err == nil {
			raw, _ := json.MarshalIndent(map[string]any{"result": result, "created_at": time.Now().In(chinaTime), "server_published": false, "game_loading_verified": false}, "", "  ")
			err = os.WriteFile(filepath.Join(directory, "密钥与校验.json"), raw, 0600)
			if err != nil {
				emergency := []byte("输出密钥：" + result.NewKeySet + "\r\n输出SHA256：" + result.OutputSHA256)
				_ = os.WriteFile(filepath.Join(localDataDirectory(), "待补存PVF密钥_"+adminapi.NewRequestID()+".txt"), emergency, 0600)
			}
		}
		p.window.Synchronize(func() {
			p.pvfBusy = false
			if err != nil {
				p.pvfOutput.SetText("本机换钥未完成：" + err.Error() + "\r\n请保留输出目录核对：" + directory)
				return
			}
			p.lastPVF = result
			p.pvfOutput.SetText(fmt.Sprintf("本机换钥与内容校验完成。\r\n输出：%s\r\nSHA256：%s\r\n密钥清单：%s\r\n\r\n未上传、更换服务器PVF或改动游戏数据库；实际游戏加载尚未验收。", result.OutputPath, result.OutputSHA256, filepath.Join(directory, "密钥与校验.json")))
			logUIAction("PVF管理", "本机PVF换钥", filepath.Base(source), "本机内容等价校验通过，未发布")
		})
	}()
}
func (p *publishingUI) buildLauncher() {
	if p.buildBusy {
		return
	}
	root, err := selectedOutputDirectory(p.launcherDirectory.Text())
	if err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	name, err := checkedExecutableName(p.launcherName.Text())
	if err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	base, err := os.Executable()
	if err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	template, err := LoadCurrentLauncherTemplate(filepath.Dir(base))
	if err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	options := LauncherPackageOptions{TemplatePath: template.Path, TemplateSHA256: template.SHA256, DisplayName: strings.TrimSuffix(name, ".exe"), ServerID: p.serverID.Text(), LoginURL: p.loginURL.Text(), CertificatePath: p.loginCertificate.Text(), OutputDir: filepath.Join(root, "登录器_"+adminapi.NewRequestID()), DirectOutput: false}
	p.buildBusy = true
	p.buildOutput.SetText("正在本机校验模板并生成登陆器…")
	go func() {
		result, err := BuildLauncherPackage(options)
		p.window.Synchronize(func() {
			p.buildBusy = false
			if err != nil {
				p.buildOutput.SetText(err.Error())
				return
			}
			p.buildOutput.SetText("原版玩家登录器已生成：\r\n" + result.LauncherPath + "\r\nSHA256：" + result.LauncherSHA256 + "\r\n\r\n沿用当前登录器的原界面、组件和配套.NET运行库。请分发整个生成目录（EXE、Image与runtime）。\r\n只包含玩家公开配置，不包含GM管理令牌。\r\n本机生成完成；未连接正式服务器或执行真实登录。")
			logUIAction("登录下载管理", "生成本机登陆器", result.LauncherPath, "本机校验通过，实际登录未验收")
		})
	}()
}
func (p *publishingUI) buildDownloader() {
	if p.buildBusy {
		return
	}
	name, err := checkedExecutableName(p.downloaderName.Text())
	if err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	root, err := selectedOutputDirectory(p.downloaderDirectory.Text())
	if err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	config := downloader.Config{Version: 1, Name: strings.TrimSuffix(name, ".exe"), URL: p.clientURL.Text(), FileName: p.archiveName.Text(), Extract: true, Launcher: strings.ReplaceAll(p.archiveLauncher.Text(), "\\", "/"), AutoStart: true}
	if err = config.Validate(); err != nil {
		p.buildOutput.SetText(err.Error())
		return
	}
	exe, _ := os.Executable()
	template := filepath.Join(filepath.Dir(exe), "下载器模板.exe")
	pinRaw, err := os.ReadFile(filepath.Join(filepath.Dir(exe), "下载器模板.SHA256.txt"))
	if err != nil {
		p.buildOutput.SetText("缺少配套下载器模板清单。")
		return
	}
	pin, err := sha256File(template)
	if err != nil || pin != strings.TrimSpace(string(pinRaw)) {
		p.buildOutput.SetText("下载器模板校验失败。")
		return
	}
	p.buildBusy = true
	p.buildOutput.SetText("正在本机生成下载器…")
	go func() {
		target, err := buildLocalDownloader(template, pin, root, name, config)
		p.window.Synchronize(func() {
			p.buildBusy = false
			if err != nil {
				p.buildOutput.SetText("下载器生成失败：" + err.Error())
				return
			}
			p.buildOutput.SetText("下载器已生成并回读配置：\r\n" + target + "\r\n\r\n生成过程中未下载、解压或启动客户端；实际下载链路待验收。")
			logUIAction("登录下载管理", "生成本机下载器", target, "模板摘要及内置配置通过")
		})
	}()
}
func buildLocalDownloader(template, pin, root, name string, config downloader.Config) (string, error) {
	if err := config.Validate(); err != nil {
		return "", err
	}
	if !validWindowsFileName(name) || !strings.EqualFold(filepath.Ext(name), ".exe") {
		return "", fmt.Errorf("下载器输出名称无效")
	}
	actual, err := sha256File(template)
	if err != nil || actual != pin {
		return "", fmt.Errorf("下载器模板摘要不匹配")
	}
	root, stage, err := flatOutputStage(root, ".downloader-stage-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(stage)
	target := filepath.Join(stage, name)
	if err = copyOutputFile(template, target); err != nil {
		return "", err
	}
	if actual, err = sha256File(target); err != nil || actual != pin {
		return "", fmt.Errorf("下载器复制后的模板摘要不匹配")
	}
	file, err := os.OpenFile(target, os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return "", err
	}
	data, _ := json.Marshal(config)
	err = launcherbundle.Append(file, data, downloader.Magic)
	if err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return "", err
	}
	if _, err = downloader.LoadExecutable(target); err != nil {
		return "", err
	}
	if _, err = commitFlatOutput(stage, root, []string{name}); err != nil {
		return "", err
	}
	return filepath.Join(root, name), nil
}
func (p *publishingUI) savePublishing() {
	if err := p.configRead.check(currentManager()); err != nil {
		p.pvfOutput.SetText(err.Error())
		return
	}
	address, err := checkedDirectURL(p.updateURL.Text())
	if err != nil {
		p.pvfOutput.SetText(err.Error())
		return
	}
	value := map[string]any{"update_url": address, "client_url": ""}
	if p.clientURL != nil && p.clientURL.Text() != "" {
		if _, err = checkedDirectURL(p.clientURL.Text()); err != nil {
			p.pvfOutput.SetText(err.Error())
			return
		}
		value["client_url"] = p.clientURL.Text()
	}
	var result adminapi.ConfigEntry
	revision := p.revision
	saveSequence := p.configRead.saveStarted(p.saveConfigButton)
	managerJob("PVF管理", "保存下载配置", func(c *adminapi.Client) error {
		return saveConfigWithClient(c, "publishing", revision, value, &result)
	}, func(err error) {
		if !p.configRead.saveFinished(saveSequence, currentManager(), err, p.saveConfigButton) {
			p.pvfOutput.SetText("保存期间连接或读取状态已变化，请到对应连接重新读取确认。")
			return
		}
		if err != nil {
			p.pvfOutput.SetText(err.Error())
			return
		}
		p.revision = result.Revision
		p.pvfOutput.SetText(fmt.Sprintf("下载地址已保存到数据库，版本：%d；当前已启用的客户端PVF版本同步使用新地址。", result.Revision))
	})
}
func (p *publishingUI) loadPublishing() {
	sequence := p.configRead.begin(p.saveConfigButton)
	p.revision = 0
	var result adminapi.ConfigEntry
	managerJob("PVF管理", "读取下载配置", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/config/publishing", "", nil, &result)
	}, func(err error) {
		if sequence != p.configRead.sequence {
			return
		}
		if err != nil {
			p.pvfOutput.SetText(err.Error())
			return
		}
		if !p.configRead.accept(sequence, currentManager(), p.saveConfigButton) {
			return
		}
		p.revision = result.Revision
		p.updateURL.SetText(stringValue(result.Value, "update_url"))
		if p.clientURL != nil {
			p.clientURL.SetText(stringValue(result.Value, "client_url"))
		}
		p.pvfOutput.SetText(fmt.Sprintf("已读取当前玩家版本使用的下载地址，管理配置版本%d。", result.Revision))
	})
}

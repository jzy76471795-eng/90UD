//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/lxn/walk"
	"newadmin/internal/adminapi"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type pvfVersion struct {
	ID        int64  `json:"id"`
	Version   string `json:"pvf_version"`
	SHA       string `json:"file_sha256"`
	Key       string `json:"key_set"`
	SourceSHA string `json:"source_sha256,omitempty"`
	URL       string `json:"download_url"`
	ClientURL string `json:"client_download_url"`
	Enabled   bool   `json:"enabled"`
	Current   bool   `json:"current"`
	Confirm   bool   `json:"confirm,omitempty"`
}
type pvfVersionPage struct {
	Items      []pvfVersion `json:"items"`
	SourceSHA  string       `json:"source_sha256"`
	SourceSize int64        `json:"source_size"`
}

func (p *publishingUI) readPVFVersions() {
	var page pvfVersionPage
	managerJob("PVF管理", "读取版本", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/pvf/releases", "", nil, &page)
	}, func(err error) {
		if err != nil {
			p.pvfOutput.SetText(err.Error())
			return
		}
		lines := []string{fmt.Sprintf("服务器源PVF：%s（%d字节）", page.SourceSHA, page.SourceSize)}
		for _, item := range page.Items {
			lines = append(lines, fmt.Sprintf("版本：%s；启用：%t；当前：%t\r\nSHA256：%s\r\n更新地址：%s", item.Version, item.Enabled, item.Current, item.SHA, item.URL))
		}
		p.pvfOutput.SetText(strings.Join(lines, "\r\n\r\n"))
	})
}
func (p *publishingUI) loadPVFResult() {
	dialog := &walk.FileDialog{Title: "选择本机PVF密钥与校验清单", Filter: "JSON (*.json)|*.json", FilePath: p.pvfDirectory.Text()}
	if ok, err := dialog.ShowOpen(p.window); err != nil {
		p.pvfOutput.SetText(err.Error())
		return
	} else if !ok {
		return
	}
	raw, err := os.ReadFile(dialog.FilePath)
	if err != nil {
		p.pvfOutput.SetText(err.Error())
		return
	}
	var doc struct {
		Result *PVFResult `json:"result"`
	}
	if json.Unmarshal(raw, &doc) != nil || doc.Result == nil || !doc.Result.Verified {
		p.pvfOutput.SetText("清单无有效的PVF校验结果。")
		return
	}
	p.lastPVF = doc.Result
	p.pvfOutput.SetText("已载入本机结果：\r\n" + doc.Result.OutputPath + "\r\n发布前会重新核对文件SHA256及密钥。")
}
func (p *publishingUI) publishPVF() {
	if p.lastPVF == nil {
		p.pvfOutput.SetText("请先执行PVF换钥，或读取本机校验清单。")
		return
	}
	result := *p.lastPVF
	version := strings.TrimSpace(p.releaseVersion.Text())
	if version == "" {
		version = time.Now().In(chinaTime).Format("20060102_150405")
		p.releaseVersion.SetText(version)
	}
	enabled := p.releaseEnabled.Checked()
	if !walkConfirm(p.window, "确认发布PVF版本", fmt.Sprintf("版本：%s\r\n文件：%s\r\nSHA256：%s\r\n启用：%t\r\n将写入玩家HTTPS使用的版本与密钥，是否继续？", version, result.OutputPath, result.OutputSHA256, enabled)) {
		return
	}
	value := pvfVersion{Version: version, SHA: result.OutputSHA256, Key: result.NewKeySet, SourceSHA: result.SourceSHA256, URL: p.updateURL.Text(), ClientURL: p.clientURL.Text(), Enabled: enabled, Confirm: true}
	var receipt adminapi.Receipt
	managerJob("PVF管理", "发布版本", func(c *adminapi.Client) error {
		sha, err := sha256File(result.OutputPath)
		if err != nil || sha != result.OutputSHA256 {
			return fmt.Errorf("本机产物已改变，未发布")
		}
		checked, err := inspectPVFFile(context.Background(), result.OutputPath, result.NewKeySet)
		if err != nil || !checked.Verified {
			return fmt.Errorf("本机产物密钥复核失败，未发布")
		}
		return c.Do(context.Background(), "POST", "/manage/v1/pvf/releases", adminapi.NewRequestID(), value, &receipt)
	}, func(err error) {
		if err != nil {
			p.pvfOutput.SetText(err.Error())
			return
		}
		p.pvfOutput.SetText(receipt.Message + "\r\n版本：" + version)
		logUIAction("PVF管理", "发布版本", version, receipt.Message)
		p.loadPublishing()
	})
}
func (p *publishingUI) disableCurrentPVF() {
	if !walkConfirm(p.window, "停用当前PVF版本", "将停止向玩家提供当前版本密钥；不会删除版本或服务器文件。确认停用？") {
		return
	}
	var receipt adminapi.Receipt
	managerJob("PVF管理", "停用当前版本", func(c *adminapi.Client) error {
		var page pvfVersionPage
		if err := c.Do(context.Background(), "GET", "/manage/v1/pvf/releases", "", nil, &page); err != nil {
			return err
		}
		for _, item := range page.Items {
			if item.Current {
				item.Enabled = false
				item.Confirm = true
				return c.Do(context.Background(), "POST", "/manage/v1/pvf/releases", adminapi.NewRequestID(), item, &receipt)
			}
		}
		return fmt.Errorf("没有当前版本")
	}, func(err error) {
		if err != nil {
			p.pvfOutput.SetText(err.Error())
			return
		}
		p.pvfOutput.SetText(receipt.Message)
	})
}
func (p *publishingUI) fillPlayerDefaults() {
	if p.loginURL != nil && p.loginURL.Text() == "" {
		p.loginURL.SetText("https://127.0.0.1:17605")
	}
	if p.loginCertificate != nil && p.loginCertificate.Text() == "" {
		p.loginCertificate.SetText(filepath.Join(localDataDirectory(), "server-cert.pem"))
	}
}

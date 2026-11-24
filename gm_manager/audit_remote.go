//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"newadmin/internal/adminapi"
)

func persistLocalAudit(entry operationEntry) {
	directory := filepath.Join(localDataDirectory(), "操作日志")
	if os.MkdirAll(directory, 0700) != nil {
		return
	}
	file, err := os.OpenFile(filepath.Join(directory, entry.when.Format("20060102")+".jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer file.Close()
	_ = json.NewEncoder(file).Encode(adminapi.Audit{At: entry.when, Actor: "本机界面", Module: entry.module, Action: entry.action, Target: entry.target, Result: entry.result, Detail: entry.detail})
}
func loadLocalAudit() {
	directory := filepath.Join(localDataDirectory(), "操作日志")
	path := filepath.Join(directory, time.Now().In(chinaTime).Format("20060102")+".jsonl")
	file, err := os.Open(path)
	if err != nil {
		return
	}
	defer file.Close()
	decoder := json.NewDecoder(file)
	for i := 0; i < 10000; i++ {
		var entry adminapi.Audit
		if decoder.Decode(&entry) != nil {
			break
		}
		managementAudit.entries = append(managementAudit.entries, operationEntry{when: entry.At, module: entry.Module, action: entry.Action, target: entry.Target, result: entry.Result, detail: entry.Detail})
	}
}
func (a *operationLogUI) queryRemote() {
	sequence := a.sequence
	if a.pageNumber < 1 {
		a.pageNumber = 1
	}
	query := url.Values{"keyword": {a.keyword.Text()}, "from": {a.from.Text()}, "to": {a.to.Text()}, "page": {strconv.Itoa(a.pageNumber)}, "page_size": {"50"}}
	if a.module.CurrentIndex() > 0 {
		query.Set("module", a.module.Text())
	}
	var page adminapi.Page[adminapi.Audit]
	client := currentManager()
	if client == nil {
		a.count.SetText("请先连接Go管理服务")
		a.model.rows = nil
		a.model.PublishRowsReset()
		return
	}
	go func() {
		err := client.Do(context.Background(), "GET", "/manage/v1/audit?"+query.Encode(), "", nil, &page)
		applicationWindow.Synchronize(func() {
			if currentManager() != client || a.sequence != sequence {
				return
			}
			a.table.SetCurrentIndex(-1)
			a.view = nil
			a.model.rows = nil
			if err != nil {
				a.count.SetText(err.Error())
				a.model.PublishRowsReset()
				return
			}
			for _, entry := range page.Items {
				a.view = append(a.view, operationEntry{when: entry.At.In(chinaTime), module: entry.Module, action: entry.Action, target: entry.Target, result: entry.Result, detail: entry.Detail + "；请求编号：" + entry.RequestID})
				a.model.rows = append(a.model.rows, []string{entry.At.In(chinaTime).Format("2006-01-02 15:04:05"), entry.Actor, entry.Module, entry.Action, entry.Target, entry.Result})
			}
			a.total = page.Total
			a.model.PublishRowsReset()
			a.count.SetText(fmt.Sprintf("Go服务审计共%d条，第%d页", page.Total, page.Page))
			a.showDetail()
		})
	}()
}

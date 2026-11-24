//go:build windows

package main

import (
	"context"
	"fmt"
	"net/url"
	"strconv"

	"newadmin/internal/adminapi"
)

func (r *recordsUI) queryRemote() {
	r.sequence++
	sequence := r.sequence
	if r.pageNumber < 1 {
		r.pageNumber = 1
	}
	kinds := []string{"all", "mail", "cera", "token", "gold", "scene", "dungeon", "chat", "best", "raid", "item", "revive", "cdk"}
	index := r.kind.CurrentIndex()
	if index < 0 || index >= len(kinds) {
		index = 0
	}
	query := url.Values{"kind": {kinds[index]}, "account": {r.account.Text()}, "keyword": {r.keyword.Text()}, "from": {r.from.Text()}, "to": {r.to.Text()}, "page": {strconv.Itoa(r.pageNumber)}, "page_size": {"50"}}
	if r.direction.CurrentIndex() > 0 && index <= 4 {
		query.Set("direction", r.direction.Text())
	}
	r.count.SetText("正在查询游戏记录…")
	var page adminapi.Page[adminapi.Record]
	managerJob("记录查询", "读取游戏记录", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/records?"+query.Encode(), "", nil, &page)
	}, func(err error) {
		if sequence != r.sequence {
			return
		}
		r.table.SetCurrentIndex(-1)
		r.view = nil
		r.model.rows = nil
		if err != nil {
			r.total = 0
			r.count.SetText(err.Error())
			r.model.PublishRowsReset()
			r.showDetail()
			return
		}
		for _, row := range page.Items {
			entry := gameRecord{when: row.At.In(chinaTime), kind: row.Kind, account: row.Account, character: row.Character, direction: row.Direction, content: row.Content, place: row.Place, result: row.Result, clearMS: row.ClearMS}
			if row.Start != nil {
				entry.start = *row.Start
			}
			if row.End != nil {
				entry.end = *row.End
			}
			r.view = append(r.view, entry)
			r.model.rows = append(r.model.rows, []string{entry.when.Format("2006-01-02 15:04:05"), entry.kind, entry.account, entry.character, entry.direction, entry.content, entry.place, formatClearTime(entry.clearMS), entry.result})
		}
		r.total = page.Total
		r.model.PublishRowsReset()
		r.count.SetText(fmt.Sprintf("共 %d 条匹配记录，第 %d 页，本页 %d 条；按筛选条件查询全部已有记录", page.Total, page.Page, len(page.Items)))
		r.showDetail()
	})
}

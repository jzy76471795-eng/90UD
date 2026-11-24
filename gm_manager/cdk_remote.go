//go:build windows

package main

import (
	"context"
	"fmt"
	"net/url"
	"newadmin/internal/adminapi"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func (c *cdkUI) generateManaged() {
	if c.generating {
		return
	}
	req, err := parseManualCDK(c.manual.Text())
	if err != nil {
		c.output.SetText(err.Error())
		return
	}
	c.generating = true
	name, reward := req.Name, req.Reward
	var codes []adminapi.Code
	managerJob("CDK管理", "生成管理码", func(client *adminapi.Client) error {
		return client.Do(context.Background(), "POST", "/manage/v1/codes", adminapi.NewRequestID(), req, &codes)
	}, func(err error) {
		c.generating = false
		if err != nil {
			c.output.SetText(err.Error())
			return
		}
		c.generatedCodes = generatedCodeStrings(codes)
		c.batchCopyStatus.SetText("")
		c.pageNumber = 1
		c.search.SetText("")
		c.status.SetCurrentIndex(0)
		c.query()
		c.output.SetText(fmt.Sprintf("已保存 %d 个CDK，可点击“复制本批CDK”。\r\n每码可兑换一次。每份奖励\r\n%s", len(codes), readReward(reward).description()))
		logUIAction("CDK管理", "生成管理码", name, fmt.Sprintf("已提交数据库%d个", len(codes)))
	})
}
func (c *cdkUI) queryManaged() {
	c.sequence++
	sequence := c.sequence
	c.copyStatus.SetText("")
	if c.pageNumber < 1 {
		c.pageNumber = 1
	}
	path := fmt.Sprintf("/manage/v1/codes?kind=cdk&keyword=%s&page=%d&page_size=50", url.QueryEscape(c.search.Text()), c.pageNumber)
	if c.status.CurrentIndex() == 1 {
		path += "&state=active"
	}
	if c.status.CurrentIndex() == 3 {
		path += "&state=revoked"
	}
	if c.status.CurrentIndex() == 2 {
		path += "&state=used"
	}
	var result adminapi.Page[adminapi.Code]
	managerJob("CDK管理", "查询管理码", func(client *adminapi.Client) error {
		return client.Do(context.Background(), "GET", path, "", nil, &result)
	}, func(err error) {
		if sequence != c.sequence {
			return
		}
		c.table.SetCurrentIndex(-1)
		c.model.rows = nil
		if err != nil {
			c.total = 0
			c.model.PublishRowsReset()
			c.count.SetText(err.Error())
			return
		}
		for _, code := range result.Items {
			c.model.rows = append(c.model.rows, cdkRow{Name: code.Name, Code: code.Code, Reward: readReward(code.Reward), State: code.State})
		}
		c.model.PublishRowsReset()
		c.total = result.Total
		c.count.SetText(fmt.Sprintf("共 %d 个管理码，第 %d 页；游戏数据库", result.Total, result.Page))
		c.showDetail()
	})
}
func (c *cdkUI) exportPage() {
	if len(c.model.rows) == 0 {
		c.output.SetText("当前没有可导出的查询结果。")
		return
	}
	if err := os.MkdirAll(localDataDirectory(), 0700); err != nil {
		c.output.SetText(err.Error())
		return
	}
	path := filepath.Join(localDataDirectory(), "CDK本页_"+time.Now().In(chinaTime).Format("20060102_150405.000")+".txt")
	lines := []string{"名称\t兑换码\t状态"}
	clean := func(v string) string { return strings.NewReplacer("\t", " ", "\r", " ", "\n", " ").Replace(v) }
	for _, row := range c.model.rows {
		lines = append(lines, clean(row.Name)+"\t"+row.Code+"\t"+codeStateText(row.State))
	}
	if err := os.WriteFile(path, []byte("\xef\xbb\xbf"+strings.Join(lines, "\r\n")), 0600); err != nil {
		c.output.SetText(err.Error())
		return
	}
	c.output.SetText("本页查询结果已导出：\r\n" + path + "\r\n已导出游戏数据库查询结果。")
	logUIAction("CDK管理", "导出本页", path, fmt.Sprintf("%d条", len(c.model.rows)))
}

func (c *cdkUI) queryRemoteRedemptions() {
	query := url.Values{"kind": {"cdk"}, "account": {c.player.Text()}, "keyword": {c.recordSearch.Text()}, "page_size": {"200"}}
	var page adminapi.Page[adminapi.Record]
	managerJob("CDK管理", "查询游戏兑换记录", func(client *adminapi.Client) error {
		return client.Do(context.Background(), "GET", "/manage/v1/records?"+query.Encode(), "", nil, &page)
	}, func(err error) {
		c.recordModel.rows = nil
		if err != nil {
			c.recordCount.SetText(err.Error())
			c.recordModel.PublishRowsReset()
			return
		}
		for _, entry := range page.Items {
			row := redemptionRow{Time: entry.At.In(chinaTime).Format("2006-01-02 15:04:05"), Name: entry.Name, Code: entry.Code, Account: entry.Account, Character: entry.Character, RewardUnknown: entry.Reward == nil}
			if entry.Reward != nil {
				row.Points = int(entry.Reward.Points)
				row.Tokens = int(entry.Reward.Tokens)
				row.Gold = int(entry.Reward.Gold)
				parts := []string{}
				for _, item := range entry.Reward.Items {
					parts = append(parts, fmt.Sprintf("%d×%d", item.ID, item.Quantity))
				}
				row.Items = strings.Join(parts, "；")
			}
			c.recordModel.rows = append(c.recordModel.rows, row)
		}
		c.recordModel.PublishRowsReset()
		c.recordCount.SetText(fmt.Sprintf("匹配%d条，本页%d条；奖励列为方案，实际分项发放需核对原回执", page.Total, len(page.Items)))
	})
}

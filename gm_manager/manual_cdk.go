package main

import (
	"fmt"
	"newadmin/internal/adminapi"
	"strconv"
	"strings"
)

func parseManualCDK(text string) (adminapi.CodeRequest, error) {
	fields := strings.Split(strings.TrimSpace(text), "/")
	var req adminapi.CodeRequest
	if len(fields) < 4 || len(fields)%2 != 0 || len(fields) > 204 {
		return req, fmt.Errorf("请按名称、物品ID/数量、点券、金币、制作数量的顺序填写，用 / 分隔")
	}
	req.Name = strings.TrimSpace(fields[0])
	if len([]rune(req.Name)) < 1 || len([]rune(req.Name)) > 100 {
		return req, fmt.Errorf("名称长度须为1至100字")
	}
	parse := func(i int, label string, min, max int64) (int64, error) {
		n, e := strconv.ParseInt(strings.TrimSpace(fields[i]), 10, 64)
		if e != nil || n < min || n > max {
			return 0, fmt.Errorf("第%d栏%s须为%d到%d的整数", i+1, label, min, max)
		}
		return n, nil
	}
	end := len(fields) - 3
	req.Kind = "cdk"
	req.Reward.Items = []adminapi.Item{}
	for i := 1; i < end; i += 2 {
		id, e := parse(i, "物品ID", 1, 4294967295)
		if e != nil {
			return req, e
		}
		qty, e := parse(i+1, "物品数量", 1, 9999)
		if e != nil {
			return req, e
		}
		req.Reward.Items = append(req.Reward.Items, adminapi.Item{ID: id, Quantity: qty})
	}
	var e error
	req.Reward.Points, e = parse(end, "点券", 0, 99999999)
	if e != nil {
		return req, e
	}
	req.Reward.Gold, e = parse(end+1, "金币", 0, 999999999)
	if e != nil {
		return req, e
	}
	qty, e := parse(end+2, "制作数量", 1, 10000)
	if e != nil {
		return req, e
	}
	req.Quantity = int(qty)
	if len(req.Reward.Items) == 0 && req.Reward.Points == 0 && req.Reward.Gold == 0 {
		return req, fmt.Errorf("普通CDK至少需要一项奖励")
	}
	return req, nil
}

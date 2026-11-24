//go:build windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"newadmin/internal/adminapi"
	"strings"
)

func (f *announcementForm) section() string {
	switch f.kind {
	case "游戏导航栏公告":
		return "announcement.navigation"
	case "游戏聊天框公告":
		return "announcement.chat"
	default:
		return "announcement.launcher"
	}
}
func (f *announcementForm) save() {
	if err := f.configRead.check(currentManager()); err != nil {
		f.output.SetText(err.Error())
		return
	}
	content := f.content.Text()
	if f.enabled.Checked() && content == "" {
		f.output.SetText("启用公告需要内容。")
		return
	}
	value := map[string]any{"enabled": f.enabled.Checked(), "content": content}
	if f.title != nil {
		value["title"] = f.title.Text()
	} else {
		value["interval"] = int(f.interval.Value())
	}
	var result adminapi.ConfigEntry
	revision := f.revision
	section := f.section()
	saveSequence := f.configRead.saveStarted(f.saveConfigButton)
	managerJob("公告管理", "保存公告配置", func(c *adminapi.Client) error {
		return saveConfigWithClient(c, section, revision, value, &result)
	}, func(err error) {
		if !f.configRead.saveFinished(saveSequence, currentManager(), err, f.saveConfigButton) {
			f.output.SetText("保存期间连接或读取状态已变化，请到对应连接重新读取确认。")
			return
		}
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		f.revision = result.Revision
		f.output.SetText(fmt.Sprintf("公告配置已保存，版本：%d。\r\n已写入游戏数据库。启用的规则由Go服务轮询执行。\r\n可关闭窗口后重新连接并读取。", result.Revision))
		logUIAction("公告管理", "保存管理配置", f.kind, "已提交数据库")
	})
}
func (f *announcementForm) load() {
	sequence := f.configRead.begin(f.saveConfigButton)
	f.revision = 0
	var result adminapi.ConfigEntry
	managerJob("公告管理", "读取公告配置", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/config/"+f.section(), "", nil, &result)
	}, func(err error) {
		if sequence != f.configRead.sequence {
			return
		}
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		if !f.configRead.accept(sequence, currentManager(), f.saveConfigButton) {
			return
		}
		f.revision = result.Revision
		if result.Revision == 0 {
			f.enabled.SetChecked(false)
			f.content.SetText("")
			if f.title != nil {
				f.title.SetText("")
			}
			if f.interval != nil {
				if f.kind == "游戏聊天框公告" {
					f.interval.SetValue(10)
				} else {
					f.interval.SetValue(30)
				}
			}
			f.output.SetText("尚无已保存配置。")
			return
		}
		f.enabled.SetChecked(boolValue(result.Value, "enabled"))
		f.content.SetText(stringValue(result.Value, "content"))
		if f.title != nil {
			f.title.SetText(stringValue(result.Value, "title"))
		} else {
			f.interval.SetValue(numberValue(result.Value, "interval"))
		}
		f.output.SetText(fmt.Sprintf("已读取数据库版本 %d；已连接实际数据库：%t", result.Revision, result.DatabaseCommitted))
	})
}
func saveConfigWithClient(c *adminapi.Client, section string, revision int64, value map[string]any, out *adminapi.ConfigEntry) error {

	return c.Do(context.Background(), "PUT", "/manage/v1/config/"+section, adminapi.NewRequestID(), map[string]any{"revision": revision, "value": value}, out)
}
func boolValue(v map[string]any, key string) bool      { result, _ := v[key].(bool); return result }
func stringValue(v map[string]any, key string) string  { result, _ := v[key].(string); return result }
func numberValue(v map[string]any, key string) float64 { result, _ := v[key].(float64); return result }
func applyReward(e *rewardEditor, value any) {
	raw, _ := json.Marshal(value)
	var reward adminapi.Reward
	if json.Unmarshal(raw, &reward) != nil {
		return
	}
	r := readReward(reward)
	e.model.items = r.Items
	e.model.PublishRowsReset()
	e.points.SetValue(float64(r.Points))
	e.tokens.SetValue(float64(r.Tokens))
	e.gold.SetValue(float64(r.Gold))
	e.changed()
}
func (f *welfareForm) section() string {
	switch f.kind {
	case "定时福利":
		return "welfare.online"
	case "每周奖励":
		return "welfare.weekly"
	default:
		return "welfare.monthly"
	}
}
func (f *welfareForm) save() {
	if err := f.configRead.check(currentManager()); err != nil {
		f.output.SetText(err.Error())
		return
	}
	reward := f.rewards.bundle()
	if err := reward.validate(100); err != nil {
		f.output.SetText(err.Error())
		return
	}
	if f.enabled.Checked() && reward.empty() {
		f.output.SetText("启用福利至少需要一项奖励。")
		return
	}
	value := map[string]any{"enabled": f.enabled.Checked(), "reward": wireReward(reward)}
	if f.clockTimes != nil {
		value["times"] = strings.TrimSpace(f.clockTimes.Text())
	}
	var result adminapi.ConfigEntry
	revision := f.revision
	section := f.section()
	saveSequence := f.configRead.saveStarted(f.saveConfigButton)
	managerJob("在线福利", "保存福利规则", func(c *adminapi.Client) error {
		return saveConfigWithClient(c, section, revision, value, &result)
	}, func(err error) {
		if !f.configRead.saveFinished(saveSequence, currentManager(), err, f.saveConfigButton) {
			f.output.SetText("保存期间连接或读取状态已变化，请到对应连接重新读取确认。")
			return
		}
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		f.revision = result.Revision
		f.output.SetText(fmt.Sprintf("福利规则已保存，版本：%d。\r\n定时福利按北京时间的在线角色发放。周/月奖励在玩家在线时每账号每周期发一次，物品寄给当前角色。", result.Revision))
		logUIAction("在线福利", "保存管理配置", f.kind, "已提交数据库规则")
	})
}
func (f *welfareForm) load() {
	sequence := f.configRead.begin(f.saveConfigButton)
	f.revision = 0
	var result adminapi.ConfigEntry
	managerJob("在线福利", "读取福利规则", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "GET", "/manage/v1/config/"+f.section(), "", nil, &result)
	}, func(err error) {
		if sequence != f.configRead.sequence {
			return
		}
		if err != nil {
			f.output.SetText(err.Error())
			return
		}
		if !f.configRead.accept(sequence, currentManager(), f.saveConfigButton) {
			return
		}
		f.revision = result.Revision
		if result.Revision == 0 {
			f.enabled.SetChecked(false)
			if f.clockTimes != nil {
				f.clockTimes.SetText("")
			}
			applyReward(f.rewards, adminapi.Reward{})
			f.output.SetText("尚无已保存配置。")
			return
		}
		f.enabled.SetChecked(boolValue(result.Value, "enabled"))
		if f.clockTimes != nil {
			times, _ := result.Value["times"].(string)
			f.clockTimes.SetText(times)
			if times == "" {
				f.enabled.SetChecked(false)
			}
		}
		applyReward(f.rewards, result.Value["reward"])
		f.output.SetText(fmt.Sprintf("已读取数据库版本 %d；已连接实际数据库：%t", result.Revision, result.DatabaseCommitted))
	})
}
func (r *registrationUI) save() {
	if err := r.configRead.check(currentManager()); err != nil {
		r.output.SetText(err.Error())
		return
	}
	reward := r.rewards.bundle()
	if err := reward.validate(100); err != nil {
		r.output.SetText(err.Error())
		return
	}
	if r.rewardEnabled.Checked() && reward.empty() {
		r.output.SetText("启用注册奖励至少需要一项奖励。")
		return
	}
	value := map[string]any{"open": r.open.Checked(), "activation_required": r.activation.Checked(), "ip_limit": int(r.ipLimit.Value()), "reward_enabled": r.rewardEnabled.Checked(), "reward": wireReward(reward)}
	var result adminapi.ConfigEntry
	revision := r.revision
	saveSequence := r.configRead.saveStarted(r.saveConfigButton)
	managerJob("注册管理", "保存注册规则", func(c *adminapi.Client) error {
		return saveConfigWithClient(c, "registration", revision, value, &result)
	}, func(err error) {
		if !r.configRead.saveFinished(saveSequence, currentManager(), err, r.saveConfigButton) {
			r.output.SetText("保存期间连接或读取状态已变化，请到对应连接重新读取确认。")
			return
		}
		if err != nil {
			r.output.SetText(err.Error())
			return
		}
		r.revision = result.Revision
		r.output.SetText(fmt.Sprintf("注册管理规则已保存，版本：%d。\r\n注册开关及激活码策略已写入游戏数据库；注册奖励仅对保存后新增账号在首次建角后发放。", result.Revision))
		logUIAction("注册管理", "保存管理配置", "注册规则", "已提交数据库规则")
	})
}
func (r *registrationUI) load() {
	sequence := r.configRead.begin(r.saveConfigButton)
	codesRevision := r.codesRevision
	r.revision = 0
	var result adminapi.ConfigEntry
	var codes adminapi.Page[adminapi.Code]
	managerJob("注册管理", "读取注册规则", func(c *adminapi.Client) error {
		if err := c.Do(context.Background(), "GET", "/manage/v1/config/registration", "", nil, &result); err != nil {
			return err
		}
		return c.Do(context.Background(), "GET", "/manage/v1/codes?kind=registration&page_size=200", "", nil, &codes)
	}, func(err error) {
		if sequence != r.configRead.sequence {
			return
		}
		if err != nil {
			r.output.SetText(err.Error())
			return
		}
		if !r.configRead.accept(sequence, currentManager(), r.saveConfigButton) {
			return
		}
		r.revision = result.Revision
		if result.Revision == 0 {
			r.open.SetChecked(true)
			r.activation.SetChecked(false)
			r.ipLimit.SetValue(3)
			r.rewardEnabled.SetChecked(false)
			applyReward(r.rewards, adminapi.Reward{})
		}
		if result.Revision > 0 {
			r.open.SetChecked(boolValue(result.Value, "open"))
			r.activation.SetChecked(boolValue(result.Value, "activation_required"))
			r.ipLimit.SetValue(numberValue(result.Value, "ip_limit"))
			r.rewardEnabled.SetChecked(boolValue(result.Value, "reward_enabled"))
			applyReward(r.rewards, result.Value["reward"])
		}
		if codesRevision == r.codesRevision {
			r.codeTable.SetCurrentIndex(-1)
			r.copyStatus.SetText("")
			r.codes.rows = nil
			for _, code := range codes.Items {
				r.codes.rows = append(r.codes.rows, []string{code.Name, code.Code, codeStateText(code.State)})
			}
			r.codes.PublishRowsReset()
			r.output.SetText(fmt.Sprintf("已读取规则版本 %d，游戏注册码共 %d 条，本页显示 %d 条。", result.Revision, codes.Total, len(codes.Items)))
		} else {
			r.output.SetText(fmt.Sprintf("已读取规则版本 %d。", result.Revision))
		}
	})
}
func (r *registrationUI) generateManagedCodes() {
	if r.generating {
		return
	}
	r.generating = true
	name := r.name.Text()
	quantity := int(r.quantity.Value())
	var codes []adminapi.Code
	managerJob("注册管理", "生成管理激活码", func(c *adminapi.Client) error {
		return c.Do(context.Background(), "POST", "/manage/v1/codes", adminapi.NewRequestID(), adminapi.CodeRequest{Kind: "registration", Name: name, Quantity: quantity}, &codes)
	}, func(err error) {
		r.generating = false
		if err != nil {
			r.output.SetText(err.Error())
			return
		}
		r.codesRevision++
		r.generatedCodes = generatedCodeStrings(codes)
		r.copyStatus.SetText("")
		rows := make([][]string, 0, len(codes)+len(r.codes.rows))
		for _, code := range codes {
			rows = append(rows, []string{code.Name, code.Code, codeStateText(code.State)})
		}
		r.codes.rows = append(rows, r.codes.rows...)
		r.codes.PublishRowsReset()
		if len(codes) > 0 {
			r.codeTable.SetCurrentIndex(0)
		}
		r.output.SetText(fmt.Sprintf("已保存 %d 个激活码，可点击“复制本批激活码”。", len(codes)))
		logUIAction("注册管理", "生成管理码", name, fmt.Sprintf("已提交数据库%d个", len(codes)))
	})
}

//go:build windows

package main

import (
	"context"
	"fmt"
	"os"
	"sync"

	"github.com/lxn/walk"
	"newadmin/internal/adminapi"
	"newadmin/internal/managerprofile"
)

var applicationWindow *walk.MainWindow
var globalAccounts *accountUI
var configurationLoads []func()
var managementConnection struct {
	sync.RWMutex
	client *adminapi.Client
}

func currentManager() *adminapi.Client {
	managementConnection.RLock()
	defer managementConnection.RUnlock()
	return managementConnection.client
}
func setManager(client *adminapi.Client) {
	managementConnection.Lock()
	old := managementConnection.client
	managementConnection.client = client
	managementConnection.Unlock()
	if old != nil {
		old.Close()
	}
}
func managerJob(module, action string, job func(*adminapi.Client) error, done func(error)) {
	client := currentManager()
	if client == nil {
		done(fmt.Errorf("请先在“服务器管理”连接Go管理服务"))
		return
	}
	go func() {
		err := job(client)
		applicationWindow.Synchronize(func() {
			if currentManager() != client {
				done(fmt.Errorf("管理连接已变化，请到原连接核对刚才的请求"))
				return
			}
			if err != nil {
				logUIAction(module, action, "管理接口", err.Error())
			}
			done(err)
		})
	}()
}
func getManager(path string, out any) error {
	client := currentManager()
	if client == nil {
		return fmt.Errorf("请先连接Go管理服务")
	}
	return client.Do(context.Background(), "GET", path, "", nil, out)
}
func saveManagerConfig(section string, revision int64, value map[string]any, out *adminapi.ConfigEntry) error {
	client := currentManager()
	if client == nil {
		return fmt.Errorf("请先连接Go管理服务")
	}
	return client.Do(context.Background(), "PUT", "/manage/v1/config/"+section, adminapi.NewRequestID(), map[string]any{"revision": revision, "value": value}, out)
}
func wireReward(r rewardBundle) adminapi.Reward {
	result := adminapi.Reward{Points: int64(r.Points), Tokens: int64(r.Tokens), Gold: int64(r.Gold), Items: []adminapi.Item{}}
	for _, item := range r.Items {
		result.Items = append(result.Items, adminapi.Item{ID: int64(item.ID), Quantity: int64(item.Quantity)})
	}
	return result
}
func readReward(r adminapi.Reward) rewardBundle {
	result := rewardBundle{Points: int(r.Points), Tokens: int(r.Tokens), Gold: int(r.Gold)}
	for _, item := range r.Items {
		result.Items = append(result.Items, rewardItem{ID: int(item.ID), Quantity: int(item.Quantity)})
	}
	return result
}
func localDataDirectory() string {
	executable, _ := os.Executable()
	return managerprofile.DataDirectory(executable, os.Getenv("US90_GM_DATA_HOME"))
}
func saveConnectionProfile(address, token, certificate string) error {
	return managerprofile.Save(localDataDirectory(), managerprofile.Profile{Address: address, Token: token, Certificate: certificate})
}

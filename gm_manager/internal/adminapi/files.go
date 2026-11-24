package adminapi

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"
)

func (c *Client) transferClient() *http.Client {
	return &http.Client{Transport: c.http.Transport, Timeout: 15 * time.Minute, CheckRedirect: c.http.CheckRedirect}
}
func (c *Client) Download(ctx context.Context, path, target string) (string, error) {
	if !strings.HasPrefix(path, "/manage/v1/") {
		return "", fmt.Errorf("下载接口无效")
	}
	req, err := http.NewRequestWithContext(ctx, "GET", c.base+path, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	response, err := c.transferClient().Do(req)
	if err != nil {
		return "", fmt.Errorf("HTTPS备份下载未完成")
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return "", fmt.Errorf("备份下载被拒绝（HTTP %d）", response.StatusCode)
	}
	expected := response.Header.Get("X-Content-SHA256")
	if len(expected) != 64 {
		return "", fmt.Errorf("备份缺少SHA256回执")
	}
	f, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return "", err
	}
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, hash), io.LimitReader(response.Body, 512<<20+1))
	if err == nil {
		err = f.Sync()
	}
	closed := f.Close()
	if err == nil {
		err = closed
	}
	actual := hex.EncodeToString(hash.Sum(nil))
	if err != nil || n > 512<<20 || expected != actual || response.ContentLength >= 0 && response.ContentLength != n {
		return "", fmt.Errorf("本机备份长度或SHA256未通过，未允许替换")
	}
	return actual, nil
}
func (c *Client) Upload(ctx context.Context, path, id, file, expected, backupSHA string, out *Receipt) error {
	if path != "/manage/v1/deployment/update" && path != "/manage/v1/pvf/source" {
		return fmt.Errorf("上传目标无效")
	}
	f, err := os.Open(file)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, "PUT", c.base+path, f)
	if err != nil {
		return err
	}
	req.ContentLength = info.Size()
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/octet-stream")
	req.Header.Set("Idempotency-Key", id)
	req.Header.Set("X-Content-SHA256", expected)
	req.Header.Set("X-Backup-SHA256", backupSHA)
	req.Header.Set("X-Content-Size", strconv.FormatInt(info.Size(), 10))
	response, err := c.transferClient().Do(req)
	if err != nil {
		return &ClientError{Status: 503, Code: "upload_unknown", Message: "上传结果未确认，请查询原请求回执", RequestID: id, Unknown: true}
	}
	defer response.Body.Close()
	var envelope struct {
		OK        bool    `json:"ok"`
		Data      Receipt `json:"data"`
		Error     *Error  `json:"error"`
		RequestID string  `json:"request_id"`
	}
	if err = json.NewDecoder(io.LimitReader(response.Body, 1<<20)).Decode(&envelope); err != nil {
		return &ClientError{Status: 502, Message: "上传回执不完整", RequestID: id, Unknown: true}
	}
	if !envelope.OK || response.StatusCode >= 400 {
		failure := &ClientError{Status: response.StatusCode, RequestID: id, Message: "上传未完成"}
		if envelope.Error != nil {
			failure.Code = envelope.Error.Code
			failure.Message = strings.ReplaceAll(envelope.Error.Message, c.token, "[已隐藏]")
			failure.Unknown = envelope.Error.Unknown
		}
		return failure
	}
	if envelope.RequestID != id {
		return &ClientError{Status: 502, Message: "上传回执编号不一致", RequestID: id, Unknown: true}
	}
	*out = envelope.Data
	return nil
}

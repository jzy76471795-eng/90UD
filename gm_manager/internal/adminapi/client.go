package adminapi

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type Client struct {
	base, token string
	http        *http.Client
}
type ClientError struct {
	Status                   int
	Code, Message, RequestID string
	Unknown                  bool
}

func (e *ClientError) Error() string {
	message := e.Message
	if e.RequestID != "" {
		message += "（请求编号：" + e.RequestID + "）"
	}
	if e.Unknown {
		message += "；结果未确认，请查询原回执，勿直接重发"
	}
	return message
}
func NewClient(address, token, certificate string) (*Client, error) {
	u, err := url.Parse(strings.TrimRight(strings.TrimSpace(address), "/"))
	if err != nil || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Path != "" || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, fmt.Errorf("请填写有效的管理接口根地址")
	}
	ip := net.ParseIP(u.Hostname())
	loopback := u.Hostname() == "localhost" || ip != nil && ip.IsLoopback()
	if u.Scheme == "http" && !loopback {
		return nil, fmt.Errorf("远程管理连接必须使用HTTPS")
	}
	if len(token) < 32 {
		return nil, fmt.Errorf("请填写至少32字符的管理令牌")
	}
	tlsConfig := &tls.Config{MinVersion: tls.VersionTLS12}
	if certificate != "" {
		raw, err := os.ReadFile(certificate)
		if err != nil {
			return nil, fmt.Errorf("无法读取信任证书")
		}
		roots, err := x509.SystemCertPool()
		if err != nil {
			roots = x509.NewCertPool()
		}
		if !roots.AppendCertsFromPEM(raw) {
			return nil, fmt.Errorf("信任证书格式无效")
		}
		tlsConfig.RootCAs = roots
	}
	transport := &http.Transport{Proxy: nil, TLSClientConfig: tlsConfig, MaxIdleConns: 16, MaxIdleConnsPerHost: 8, IdleConnTimeout: 60 * time.Second, ResponseHeaderTimeout: 12 * time.Second}
	return &Client{base: u.String(), token: token, http: &http.Client{Transport: transport, Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}
func NewRequestID() string {
	raw := make([]byte, 16)
	if _, err := rand.Read(raw); err != nil {
		return fmt.Sprintf("request-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(raw)
}
func (c *Client) Close()           { c.http.CloseIdleConnections() }
func (c *Client) Endpoint() string { return c.base }
func (c *Client) Do(ctx context.Context, method, path, id string, body, out any) error {
	readOnly := method == "GET" || method == "POST" && path == "/manage/v1/preview"
	if !strings.HasPrefix(path, "/manage/v1/") || strings.HasPrefix(path, "//") {
		return fmt.Errorf("管理接口路径无效")
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	if len(raw) > 2<<20 {
		return fmt.Errorf("请求内容超过2MiB")
	}
	if method == "GET" {
		raw = nil
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, bytes.NewReader(raw))
	if err != nil {
		return fmt.Errorf("管理请求格式无效")
	}
	req.Header.Set("Authorization", "Bearer "+c.token)
	req.Header.Set("Content-Type", "application/json")
	if !readOnly {
		if id == "" {
			return fmt.Errorf("写入请求缺少请求编号")
		}
		req.Header.Set("Idempotency-Key", id)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return &ClientError{Status: 503, Code: "connection_failed", Message: "管理连接未返回确认，请检查地址、证书和连接状态", RequestID: id, Unknown: !readOnly}
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 32<<20+1))
	if err != nil || len(data) > 32<<20 {
		return &ClientError{Status: 502, Code: "incomplete_response", Message: "管理响应不完整", RequestID: id, Unknown: !readOnly}
	}
	var envelope struct {
		OK        bool            `json:"ok"`
		Data      json.RawMessage `json:"data"`
		Error     *Error          `json:"error"`
		RequestID string          `json:"request_id"`
	}
	if json.Unmarshal(data, &envelope) != nil {
		return &ClientError{Status: 502, Code: "invalid_response", Message: "管理服务响应格式不匹配", RequestID: id, Unknown: !readOnly}
	}
	if !readOnly && envelope.OK && envelope.RequestID != id {
		return &ClientError{Status: 502, Code: "receipt_mismatch", Message: "操作回执编号不匹配", RequestID: id, Unknown: true}
	}
	if !envelope.OK || resp.StatusCode >= 400 {
		e := &ClientError{Status: resp.StatusCode, Code: "request_failed", Message: "管理请求未完成", RequestID: envelope.RequestID}
		if envelope.Error != nil {
			e.Code = envelope.Error.Code
			e.Message = strings.ReplaceAll(envelope.Error.Message, c.token, "[已隐藏]")
			e.Unknown = envelope.Error.Unknown
		}
		return e
	}
	if out != nil && len(envelope.Data) > 0 {
		if err = json.Unmarshal(envelope.Data, out); err != nil {
			return &ClientError{Status: 502, Code: "invalid_data", Message: "管理业务响应结构不匹配", RequestID: id, Unknown: !readOnly}
		}
	}
	return nil
}

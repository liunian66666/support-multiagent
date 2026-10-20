package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"customer_service/internal/model"

	"gopkg.in/yaml.v3"
	"gorm.io/driver/mysql"
	"gorm.io/gorm"
)

// sanitizeEnvKey 将 app_key 规范化为环境变量片段（非字母数字 → 下划线）
func sanitizeEnvKey(s string) string {
	var b strings.Builder
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		} else {
			b.WriteRune('_')
		}
	}
	return b.String()
}

// Config 全局配置（P4-4：统一配置管理）
// 支持 YAML 配置文件 + 环境变量覆盖（环境变量优先）
type Config struct {
	Mysql struct {
		Dsn string `yaml:"dsn"`
	} `yaml:"mysql"`
	AgentURL string `yaml:"agent_url"`
	Server   struct {
		Port string `yaml:"port"`
	} `yaml:"server"`
	Auth AuthConfig `yaml:"auth"`
	// P4-4：数据库连接池配置
	DBPool struct {
		MaxOpenConns    int `yaml:"max_open_conns"`
		MaxIdleConns    int `yaml:"max_idle_conns"`
		ConnMaxLifetime int `yaml:"conn_max_lifetime_minutes"`
		ConnMaxIdleTime int `yaml:"conn_max_idle_minutes"`
	} `yaml:"db_pool"`
	// P4-4：内部接口鉴权 Token（Go <-> Agent 内部通信）
	InternalAPIToken string `yaml:"internal_api_token"`
	// P1-4：消息归档保留期（天，默认 90）
	MsgRetentionDays int `yaml:"msg_retention_days"`
}

// AuthConfig 鉴权与租户配置
type AuthConfig struct {
	Enabled  bool           `yaml:"enabled"`
	TokenTTL string         `yaml:"token_ttl"`
	Tenants  []model.Tenant `yaml:"tenants"`
	// DemoCustomerLogin 演示客户登录开关（默认 false，仅演示环境开启）：
	// true 时开放 /api/auth/customer-token，用【演示客户凭证】+ 白名单 customer_id
	// 签发仅含虚构客户/订单的短期客户会话令牌。真实接入时必须关闭，
	// 改为校验商家现有登录凭证后再签发。
	DemoCustomerLogin bool `yaml:"demo_customer_login"`
	// MerchantPreviewLogin 允许商家管理密钥换取受限的客户预览会话；仅测试环境开启。
	MerchantPreviewLogin bool `yaml:"merchant_preview_login"`
	// DemoGuestChat 仅用于演示栈的免登录 FAQ/转人工；访客不得查询订单。
	DemoGuestChat     bool   `yaml:"demo_guest_chat"`
	DemoGuestTenantID string `yaml:"demo_guest_tenant_id"`
	// DemoCustomers 演示客户凭证（与管理端 app_key/app_secret 完全分离）：
	// 管理密钥不得用于客户登录，客户凭证也只能选择白名单内的虚构客户。
	DemoCustomers []DemoCustomer `yaml:"demo_customers"`
}

// DemoCustomer 演示客户凭证（仅演示客户登录使用）
type DemoCustomer struct {
	TenantID  string   `yaml:"tenant_id"`  // 所属租户
	AppKey    string   `yaml:"app_key"`    // 演示客户 app_key（不得与管理端 app_key 相同）
	AppSecret string   `yaml:"app_secret"` // 占位符；实际值由 env 注入（见 LoadConfig）
	Customers []string `yaml:"customers"`  // 可演示选择的虚构客户白名单（空=拒绝所有登录）
}

var GlobalConfig Config
var DB *gorm.DB

// LoadConfig 加载配置：YAML 文件 + 环境变量覆盖
func LoadConfig(path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := yaml.Unmarshal(data, &GlobalConfig); err != nil {
		return err
	}

	// P4-4：统一环境变量覆盖（容器化部署优先使用环境变量）
	if v := os.Getenv("CS_MYSQL_DSN"); v != "" {
		GlobalConfig.Mysql.Dsn = v
	}
	if v := os.Getenv("CS_AGENT_URL"); v != "" {
		GlobalConfig.AgentURL = v
	}
	if v := os.Getenv("INTERNAL_API_TOKEN"); v != "" {
		GlobalConfig.InternalAPIToken = v
	}
	// 连接池配置环境变量覆盖
	if v := os.Getenv("CS_DB_MAX_OPEN_CONNS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			GlobalConfig.DBPool.MaxOpenConns = n
		}
	}
	if v := os.Getenv("CS_DB_MAX_IDLE_CONNS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			GlobalConfig.DBPool.MaxIdleConns = n
		}
	}
	// P1-4：消息保留期环境变量覆盖
	if v := os.Getenv("CS_MSG_RETENTION_DAYS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			GlobalConfig.MsgRetentionDays = n
		}
	}
	// 演示客户登录开关（CS_AUTH_DEMO_CUSTOMER_LOGIN=true/false）
	if v := os.Getenv("CS_AUTH_DEMO_CUSTOMER_LOGIN"); v != "" {
		GlobalConfig.Auth.DemoCustomerLogin = v == "true" || v == "1"
	}
	if v := os.Getenv("CS_AUTH_MERCHANT_PREVIEW_LOGIN"); v != "" {
		GlobalConfig.Auth.MerchantPreviewLogin = v == "true" || v == "1"
	}
	if v := os.Getenv("CS_AUTH_DEMO_GUEST_CHAT"); v != "" {
		GlobalConfig.Auth.DemoGuestChat = v == "true" || v == "1"
	}
	if v := os.Getenv("CS_AUTH_DEMO_GUEST_TENANT_ID"); v != "" {
		GlobalConfig.Auth.DemoGuestTenantID = v
	}
	// 演示客户凭证 secret 环境变量覆盖（DEMO_CUSTOMER_{APP_KEY大写}_SECRET，同租户 secret 模式）
	for i := range GlobalConfig.Auth.DemoCustomers {
		e := &GlobalConfig.Auth.DemoCustomers[i]
		if e.AppKey == "" {
			continue
		}
		if v := os.Getenv("DEMO_CUSTOMER_" + strings.ToUpper(sanitizeEnvKey(e.AppKey)) + "_SECRET"); v != "" {
			e.AppSecret = v
		}
	}

	// P4-4：配置默认值
	if GlobalConfig.DBPool.MaxOpenConns == 0 {
		GlobalConfig.DBPool.MaxOpenConns = 25
	}
	if GlobalConfig.DBPool.MaxIdleConns == 0 {
		GlobalConfig.DBPool.MaxIdleConns = 10
	}
	if GlobalConfig.DBPool.ConnMaxLifetime == 0 {
		GlobalConfig.DBPool.ConnMaxLifetime = 60 // 60分钟
	}
	if GlobalConfig.DBPool.ConnMaxIdleTime == 0 {
		GlobalConfig.DBPool.ConnMaxIdleTime = 30 // 30分钟
	}
	if GlobalConfig.Server.Port == "" {
		GlobalConfig.Server.Port = ":8080"
	}

	return nil
}

// ValidateConfig P4-4：配置验证，启动时检查关键配置
func ValidateConfig() error {
	if GlobalConfig.Mysql.Dsn == "" {
		return fmt.Errorf("MySQL DSN 未配置（CS_MYSQL_DSN 或 config.yaml mysql.dsn）")
	}
	if GlobalConfig.AgentURL == "" {
		return fmt.Errorf("Agent URL 未配置（CS_AGENT_URL 或 config.yaml agent_url）")
	}
	return nil
}

func InitDB() error {
	db, err := gorm.Open(mysql.Open(GlobalConfig.Mysql.Dsn), &gorm.Config{})
	if err != nil {
		return fmt.Errorf("mysql connect err: %w", err)
	}

	// 获取底层 *sql.DB 对象，设置连接池参数（P4-4：使用配置值）
	sqlDB, err := db.DB()
	if err != nil {
		return fmt.Errorf("get sql db err: %w", err)
	}

	sqlDB.SetMaxOpenConns(GlobalConfig.DBPool.MaxOpenConns)
	sqlDB.SetMaxIdleConns(GlobalConfig.DBPool.MaxIdleConns)
	sqlDB.SetConnMaxLifetime(time.Duration(GlobalConfig.DBPool.ConnMaxLifetime) * time.Minute)
	sqlDB.SetConnMaxIdleTime(time.Duration(GlobalConfig.DBPool.ConnMaxIdleTime) * time.Minute)

	DB = db

	// 自动建表：会话消息/工单/租户/审计/平台配置/模型提供商/归档消息/工单日志/订单/客户会话
	// 新项目结构（与 deploy/production/init/mysql/01_schema.sql 对齐）：
	// 模型已含 tenant_id、租户配额、provider_id 等全部列与索引，AutoMigrate 幂等建全。
	if err := DB.AutoMigrate(&model.Message{}, &model.Ticket{}, &model.TenantRow{},
		&model.AuditLog{}, &model.PlatformConfig{}, &model.ModelProvider{},
		&model.ArchivedMessage{}, &model.TicketLog{}, &model.Order{},
		&model.CustomerSession{},
		&model.CustomerProfile{}, &model.User{}, &model.Role{}, &model.UserRole{},
		&model.QuickReply{}, &model.AgentStatus{}, &model.SatisfactionRating{}); err != nil {
		return fmt.Errorf("auto migrate err: %w", err)
	}
	// 种子租户：tenants 表为空时从 config.yaml 写入（app_secret 哈希，支持 env 覆盖）
	if err := seedTenants(); err != nil {
		return fmt.Errorf("seed tenants err: %w", err)
	}
	return nil
}

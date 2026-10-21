// 多端接入代码示例（单独文件，避免 </script> 与页面冲突）
window.CODE_EXAMPLES = {
  pc: `<!-- ============================================ -->
<!-- PC 网站接入：引入 SDK，自动渲染浮窗按钮       -->
<!-- ============================================ -->

<!-- 方式一：最简单，data-* 自动初始化 -->
<script src="https://你的客服域名/sdk/cs-chat.js"
        data-server="https://你的客服域名"
        data-token="服务端换取的access_token"
        data-title="在线客服"
        data-theme="#1890ff"
        data-button-icon="💬"
        data-button-text="客服">
</script>

<!-- 方式二：手动初始化（推荐，可调用 API） -->
<script src="https://你的客服域名/sdk/cs-chat.js" data-manual="true"></script>
<script>
  // 从你的服务端获取【客户会话令牌】：宿主后端先校验访客登录态，
  // 再调用客服系统签发客户会话（演示模式：/py-api/api/auth/customer-token；
  // 生产模式：校验商家登录凭证后签发同结构会话）。app_secret 只存在服务端。
  async function initCustomerService() {
    const resp = await fetch('/api/get-cs-token');
    const data = await resp.json();

    CSCHAT.init({
      server: 'https://你的客服域名',
      token: data.token,              // 服务端签发的客户会话令牌
      title: '在线客服',
      theme: '#1890ff',
      position: 'right',              // right / left
      width: 380,
      height: 560,
      button_icon: '💬',
      button_text: '客服'
    });

    // 事件监听
    CSCHAT.on('message', function(msg) {
      console.log('收到消息:', msg.role, msg.content);
    });
    CSCHAT.on('handoff', function(data) {
      console.log('转人工:', data.ticket_no);
    });
  }
  initCustomerService();

  // API 调用示例
  // CSCHAT.open();              // 打开客服窗口
  // CSCHAT.close();             // 关闭客服窗口
  // CSCHAT.sendText('你好');    // 主动发送消息
  // CSCHAT.setToken(newToken);  // 更新 token
</script>`,

  h5: `<!-- ============================================ -->
<!-- H5 网页接入：跳转全屏客服页面 h5/chat.html    -->
<!-- ============================================ -->

<!-- 1. 页面上放一个"联系客服"入口 -->
<a href="javascript:openCustomerService()" class="cs-entry">
  💬 联系客服
</a>

<script>
// 演示环境：免登录咨询，查询演示订单时页面会提供登录入口。
// 生产客户身份由宿主系统后端验证并通过安全会话桥接，不放进 URL。
function openCustomerService() {
  window.location.href = 'https://你的客服域名/chat/h5.html';
}
</script>`,

  android: `// ============================================
// Android App 接入：WebView 加载 h5/chat.html
// ============================================

// CustomerServiceActivity.java
package com.yourcompany.app;

import android.annotation.SuppressLint;
import android.os.Bundle;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import androidx.appcompat.app.AppCompatActivity;
import java.net.URLEncoder;

public class CustomerServiceActivity extends AppCompatActivity {
    private WebView webView;
    private static final String CS_BASE_URL = "https://你的客服域名";

    @SuppressLint("SetJavaScriptEnabled")
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_customer_service);

        webView = findViewById(R.id.webview);

        // WebView 配置（必须开启 JS 和 DOM 存储）
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);

        webView.setWebViewClient(new WebViewClient());
        webView.setWebChromeClient(new WebChromeClient());

        // 注入 JSBridge（让 H5 能调用原生方法，如关闭 WebView）
        webView.addJavascriptInterface(new JsBridge(), "AndroidBridge");

        // 演示：免登录咨询；生产不要把客户令牌放进 URL。
        webView.loadUrl(CS_BASE_URL + "/chat/h5.html");
    }

    // JSBridge：H5 点击返回按钮时关闭 Activity
    public class JsBridge {
        @android.webkit.JavascriptInterface
        public void close() {
            runOnUiThread(() -> finish());
        }
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) webView.goBack();
        else super.onBackPressed();
    }
}`,

  ios: `// ============================================
// iOS App 接入：WKWebView 加载 h5/chat.html
// ============================================

// CustomerServiceViewController.swift
import UIKit
import WebKit

class CustomerServiceViewController: UIViewController {
    var webView: WKWebView!
    let CS_BASE_URL = "https://你的客服域名"

    override func viewDidLoad() {
        super.viewDidLoad()
        title = "在线客服"

        // WKWebView 配置
        let config = WKWebViewConfiguration()
        config.preferences.javaScriptEnabled = true

        // 注入 JSBridge（H5 调用原生方法）
        let userContentController = WKUserContentController()
        userContentController.add(self, name: "csClose")
        config.userContentController = userContentController

        webView = WKWebView(frame: view.bounds, configuration: config)
        webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        webView.navigationDelegate = self
        view.addSubview(webView)

        // 演示：免登录咨询；生产不要把客户令牌放进 URL。
        if let url = URL(string: "\\(CS_BASE_URL)/chat/h5.html") {
            webView.load(URLRequest(url: url))
        }
    }
}

// MARK: - WKScriptMessageHandler（JSBridge）
extension CustomerServiceViewController: WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController,
                                didReceive message: WKScriptMessage) {
        if message.name == "csClose" {
            // H5 点击返回按钮时，关闭当前 ViewController
            navigationController?.popViewController(animated: true)
        }
    }
}

extension CustomerServiceViewController: WKNavigationDelegate {}`,

  mini: `<!-- ============================================ -->
<!-- 微信小程序接入：web-view 加载 h5/chat.html    -->
<!-- ============================================ -->

<!-- 1. 页面结构 customer-service.wxml -->
<web-view src="{{csUrl}}" bindmessage="onMessage"></web-view>

<!-- 2. 页面逻辑 customer-service.js -->
Page({
  data: {
    csUrl: ''
  },

  onLoad() {
    this.loadCustomerService();
  },

  async loadCustomerService() {
    try {
      // 演示：免登录咨询；查询订单时页面引导登录。
      this.setData({ csUrl: 'https://你的客服域名/chat/h5.html' });
    } catch (err) {
      wx.showToast({ title: '客服暂时不可用', icon: 'none' });
    }
  },

  // 接收 web-view 发来的消息（用户点击关闭时触发）
  onMessage(e) {
    const msg = e.detail.data[e.detail.data.length - 1];
    if (msg && msg.type === 'cs_close') {
      wx.navigateBack();  // 返回小程序上一页
    }
  }
});

<!-- 3. 页面配置 customer-service.json -->
{
  "navigationBarTitleText": "在线客服",
  "navigationBarBackgroundColor": "#1890ff",
  "navigationBarTextStyle": "white"
}

<!-- 4. 从其他页面跳转 -->
wx.navigateTo({
  url: '/pages/customer-service/customer-service'
});

<!-- ⚠️ 前置配置：微信公众平台 → 开发 → 业务域名，添加客服系统域名 -->`
};

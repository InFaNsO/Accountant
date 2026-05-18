package com.applestreeabrasives.accountant

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar
import android.widget.RelativeLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var progressBar: ProgressBar

    private val notificationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* permission result — FCM will still deliver data messages regardless */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Edge-to-edge: content draws behind status bar; we handle insets explicitly
        WindowCompat.setDecorFitsSystemWindows(window, false)
        setContentView(R.layout.activity_main)

        // White status bar with dark icons to match the web app topbar
        @Suppress("DEPRECATION")
        window.statusBarColor = android.graphics.Color.parseColor("#FFFFFF")
        WindowInsetsControllerCompat(window, window.decorView)
            .isAppearanceLightStatusBars = true

        webView     = findViewById(R.id.webView)
        swipeRefresh = findViewById(R.id.swipeRefresh)
        progressBar = findViewById(R.id.progressBar)

        // Push layout content below the status bar
        val rootLayout = findViewById<RelativeLayout>(R.id.rootLayout)
        ViewCompat.setOnApplyWindowInsetsListener(rootLayout) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.statusBars())
            v.setPadding(0, bars.top, 0, 0)
            WindowInsetsCompat.CONSUMED
        }

        requestNotificationPermission()
        setupWebView()

        // If opened from a notification with a deep-link URL, navigate there
        val deepUrl = intent.getStringExtra("url")
        val startUrl = if (!deepUrl.isNullOrBlank())
            "${SERVER_URL}${deepUrl}"
        else
            SERVER_URL

        webView.loadUrl(startUrl)

        swipeRefresh.setOnRefreshListener { webView.reload() }
    }

    private fun setupWebView() {
        val cookieManager = CookieManager.getInstance()
        cookieManager.setAcceptCookie(true)
        cookieManager.setAcceptThirdPartyCookies(webView, true)

        webView.settings.apply {
            javaScriptEnabled      = true
            domStorageEnabled      = true
            databaseEnabled        = true
            loadWithOverviewMode   = true
            useWideViewPort        = true
            setSupportZoom(false)
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                progressBar.visibility = View.VISIBLE
                swipeRefresh.isRefreshing = false
            }

            override fun onPageFinished(view: WebView, url: String) {
                progressBar.visibility = View.GONE
                swipeRefresh.isRefreshing = false

                // Register FCM token once logged in (not on login page)
                if (!url.contains("/login") && !url.contains("/auth")) {
                    CoroutineScope(Dispatchers.IO).launch {
                        TokenRegistrationHelper.registerIfNeeded(
                            this@MainActivity,
                            url
                        )
                    }
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                // Keep all navigation inside the WebView (same host)
                val host = request.url.host ?: return false
                return !host.contains("applestreeabrasives.com")
            }
        }
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    companion object {
        const val SERVER_URL = "https://admin.applestreeabrasives.com"
    }
}

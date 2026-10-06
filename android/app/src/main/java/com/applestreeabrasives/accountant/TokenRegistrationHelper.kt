package com.applestreeabrasives.accountant

import android.content.Context
import android.webkit.CookieManager
import com.google.firebase.messaging.FirebaseMessaging
import kotlinx.coroutines.tasks.await
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

object TokenRegistrationHelper {

    private const val PREF_FILE   = "fcm_prefs"
    private const val PREF_TOKEN  = "registered_token"
    private const val REGISTER_PATH = "/api/mobile/register-token"

    private val http = OkHttpClient()

    /** Called after every page-load when user is logged in. Re-registers only if token changed. */
    suspend fun registerIfNeeded(context: Context, currentUrl: String) {
        try {
            val token = FirebaseMessaging.getInstance().token.await()
            val prefs = context.getSharedPreferences(PREF_FILE, Context.MODE_PRIVATE)
            val stored = prefs.getString(PREF_TOKEN, null)
            if (token == stored) return  // already registered this token

            register(context, token, currentUrl)

            if (isSuccessful(context, token, currentUrl)) {
                prefs.edit().putString(PREF_TOKEN, token).apply()
            }
        } catch (e: Exception) {
            // Silently ignore — will retry on next page load
        }
    }

    /** Called by MyFirebaseMessagingService when the token rotates. */
    fun registerNewToken(context: Context, token: String) {
        // Clear stored token so registerIfNeeded sends on next page load
        context.getSharedPreferences(PREF_FILE, Context.MODE_PRIVATE)
            .edit().remove(PREF_TOKEN).apply()
    }

    private fun isSuccessful(context: Context, token: String, pageUrl: String): Boolean {
        return try {
            val cookie = CookieManager.getInstance()
                .getCookie(MainActivity.SERVER_URL) ?: return false

            val json = """{"fcm_token":"$token"}"""
            val body = json.toRequestBody("application/json".toMediaType())

            val request = Request.Builder()
                .url("${MainActivity.SERVER_URL}$REGISTER_PATH")
                .addHeader("Cookie", cookie)
                .addHeader("X-Requested-With", "XMLHttpRequest")
                .post(body)
                .build()

            http.newCall(request).execute().use { it.isSuccessful }
        } catch (e: IOException) {
            false
        }
    }

    private suspend fun register(context: Context, token: String, pageUrl: String) {
        isSuccessful(context, token, pageUrl)
    }
}

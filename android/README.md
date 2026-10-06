# Android App — Setup Guide

## One-time Firebase setup (do this before building)

1. Go to https://console.firebase.google.com → **Create project** → name it `applesabrasives-accountant`.
2. Inside the project, click **Add app** → **Android**.
3. Package name: `com.applestreeabrasives.accountant`
4. Download `google-services.json` and place it at:
   ```
   android/app/google-services.json
   ```
5. Go to **Project Settings → Service accounts → Generate new private key** → download the JSON.
6. Upload it to the production server:
   ```
   scp firebase-credentials.json ledger@<server-ip>:/home/ledger/app/firebase-credentials.json
   chmod 600 /home/ledger/app/firebase-credentials.json
   ```
7. Add to `/home/ledger/app/.env` on the server:
   ```
   FIREBASE_CREDENTIALS_PATH=/home/ledger/app/firebase-credentials.json
   ```
8. Restart the server: `sudo systemctl restart ledger`

## Building the app

Open the `android/` folder in **Android Studio** (File → Open → select this folder).

Android Studio will sync Gradle automatically. Then:
- **Run on device**: Connect a physical Android device, click Run ▶
- **Build APK**: Build → Build Bundle(s)/APK(s) → Build APK(s)

> **Note:** You need `google-services.json` in `app/` before Gradle will sync successfully.

## How it works

- The app opens `https://admin.applestreeabrasives.com` in a full-screen WebView.
- Any features added to the website are **instantly available** in the app — no app update needed.
- After login, the app silently registers the device's FCM token with the server.
- The server checks for overdue invoices and low stock every hour and sends push notifications.
- Tapping a notification opens the app and navigates to the relevant section.

## Adding launcher icons

Replace the default icons with your own:
- `app/src/main/res/mipmap-*/ic_launcher.png` — standard icon
- `app/src/main/res/mipmap-*/ic_launcher_round.png` — round icon

Use Android Studio's **Image Asset Studio** (right-click `res` → New → Image Asset) for easiest generation.

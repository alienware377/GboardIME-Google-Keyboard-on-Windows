package com.gboardrelay;

import android.app.admin.DeviceAdminReceiver;

/**
 * Device-admin receiver so the relay app can be promoted to Device Owner
 * (via `adb shell dpm set-device-owner com.gboardrelay/.RelayAdminReceiver`).
 *
 * Being Device Owner lets the app whitelist itself for Lock Task (kiosk) mode,
 * which disables Home, Recents, and the gesture-nav swipe-up — so the keyboard
 * can't be accidentally swiped away or exited.
 */
public class RelayAdminReceiver extends DeviceAdminReceiver {
}

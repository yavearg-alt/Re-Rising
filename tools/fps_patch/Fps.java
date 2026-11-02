package com.saoub.fps;

import android.app.Activity;
import android.content.Context;
import android.os.Build;
import android.util.Log;
import android.view.Display;
import android.view.Window;
import android.view.WindowManager;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.InputStreamReader;

/**
 * Frame-rate unlock (tools/fps_patch.py): the game is cocos2d-x, capped at 60 fps by
 * org.cocos2dx.lib.Cocos2dxRenderer.sAnimationInterval (onDrawFrame sleeps when the interval is above 1/60 s, and
 * otherwise renders at the display's refresh rate).
 *
 * Two smali hooks call this class:
 *   - Cocos2dxActivity.onCreate -> apply(activity): asks the display for the target refresh rate (120 Hz screens
 *     often run apps at 60 Hz unless the window requests more);
 *   - Cocos2dxRenderer.setAnimationInterval(seconds) -> interval(seconds): never slower than the target.
 *
 * Target, read once: Android/data/com.bandainamcoent.saoars/files/saoub_fps.txt (written by the SAOUB Launcher
 * through adb), else assets/saoub_fps.txt (build default, fps_patch.py --fps), else 0 = the game's own behaviour.
 */
public final class Fps {
    private static final String TAG = "SAOUB";
    private static final String FILE = "saoub_fps.txt";
    private static volatile int target = -1;

    private Fps() {
    }

    public static void apply(Activity activity) {
        try {
            int fps = target(activity);
            if (fps <= 60) {
                return;
            }
            Window window = activity.getWindow();
            WindowManager.LayoutParams lp = window.getAttributes();
            lp.preferredRefreshRate = fps;
            if (Build.VERSION.SDK_INT >= 23) {
                Display display = activity.getWindowManager().getDefaultDisplay();
                Display.Mode current = display.getMode();
                Display.Mode best = current;
                for (Display.Mode m : display.getSupportedModes()) {
                    if (m.getPhysicalWidth() != current.getPhysicalWidth()
                            || m.getPhysicalHeight() != current.getPhysicalHeight()) {
                        continue;
                    }
                    // the highest rate not above the target (+1 for 119.88 Hz panels), else keep the current one
                    if (m.getRefreshRate() <= fps + 1 && m.getRefreshRate() > best.getRefreshRate()) {
                        best = m;
                    }
                }
                lp.preferredDisplayModeId = best.getModeId();
                Log.i(TAG, "fps: target " + fps + ", display mode " + best.getRefreshRate() + " Hz");
            }
            window.setAttributes(lp);
        } catch (Throwable t) {
            Log.w(TAG, "fps: apply failed", t);
        }
    }

    public static float interval(float seconds) {
        int fps = target;  // set by apply(), which runs before the GL thread starts
        if (fps <= 0) {
            return seconds;
        }
        return Math.min(seconds, 1f / fps);
    }

    private static synchronized int target(Context ctx) {
        if (target >= 0) {
            return target;
        }
        int fps = 0;
        try {
            File dir = ctx.getExternalFilesDir(null);  // also creates the folder the launcher pushes into
            File f = dir == null ? null : new File(dir, FILE);
            if (f != null && f.isFile()) {
                fps = parse(new FileInputStream(f));
            } else {
                fps = parse(ctx.getAssets().open(FILE));
            }
        } catch (Throwable ignored) {
            // no file, no asset: the game's own frame rate
        }
        target = fps >= 30 && fps <= 240 ? fps : 0;
        return target;
    }

    private static int parse(InputStream in) throws Exception {
        try (BufferedReader r = new BufferedReader(new InputStreamReader(in, "UTF-8"))) {
            String line = r.readLine();
            return line == null ? 0 : Integer.parseInt(line.trim());
        }
    }
}

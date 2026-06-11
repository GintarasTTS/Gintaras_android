package lt.gintaras.tts;

import android.app.Application;
import com.google.android.material.color.DynamicColors;

/** Enables Material You dynamic colour on Android 12+; amber fallback on older versions. */
public class GintarasApp extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        DynamicColors.applyToActivitiesIfAvailable(this);
    }
}

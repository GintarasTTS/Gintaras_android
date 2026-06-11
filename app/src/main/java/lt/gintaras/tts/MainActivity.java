package lt.gintaras.tts;

import android.content.Intent;
import android.os.Bundle;
import androidx.appcompat.app.AppCompatActivity;

/**
 * Transparent forwarder: immediately opens SettingsActivity.
 * No launcher icon is declared, so the app is not visible in the home-screen grid;
 * users reach settings via the gear icon in the system TTS settings page.
 */
public class MainActivity extends AppCompatActivity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        startActivity(new Intent(this, SettingsActivity.class));
        finish();
    }
}

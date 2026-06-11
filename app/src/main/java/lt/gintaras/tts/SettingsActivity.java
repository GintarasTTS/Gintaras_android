package lt.gintaras.tts;

import android.os.Bundle;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.preference.Preference;
import androidx.preference.PreferenceFragmentCompat;

/** Engine settings: voice selection, number processing, and update check. */
public class SettingsActivity extends AppCompatActivity {

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (savedInstanceState == null) {
            getSupportFragmentManager().beginTransaction()
                    .replace(android.R.id.content, new SettingsFragment())
                    .commit();
        }
        if (getSupportActionBar() != null) {
            getSupportActionBar().setTitle(R.string.general_settings);
        }
    }

    public static class SettingsFragment extends PreferenceFragmentCompat {
        @Override
        public void onCreatePreferences(Bundle savedInstanceState, String rootKey) {
            setPreferencesFromResource(R.xml.root_preferences, rootKey);

            // Update check — stub: always reports no updates available.
            Preference upd = findPreference("check_updates");
            if (upd != null) {
                upd.setOnPreferenceClickListener(p -> {
                    Toast.makeText(requireContext(),
                            R.string.updates_none, Toast.LENGTH_SHORT).show();
                    return true;
                });
            }
        }
    }
}

package lt.gintaras.tts;

import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.os.Bundle;
import android.widget.EditText;

import androidx.appcompat.app.AlertDialog;
import androidx.appcompat.app.AppCompatActivity;
import androidx.preference.Preference;
import androidx.preference.PreferenceCategory;
import androidx.preference.PreferenceFragmentCompat;

/** Engine settings: voice selection, number processing, about info, and (debug builds only) the
 *  synthesis self-test. */
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
            // The "Diagnostika" self-test is a developer tool. R.bool.show_diagnostics is false in
            // app/src/main and overridden to true in app/src/debug, so RELEASE builds drop the whole
            // category here and debug builds keep it.
            PreferenceCategory diagCat = findPreference("cat_diag");
            if (!getResources().getBoolean(R.bool.show_diagnostics)) {
                if (diagCat != null) getPreferenceScreen().removePreference(diagCat);
                return;
            }
            Preference diag = findPreference("run_diag");
            if (diag != null) {
                diag.setOnPreferenceClickListener(p -> { runDiagnostic(); return true; });
            }
        }

        /** Runs the on-device synthesis self-test on a background thread, then shows the report. */
        private void runDiagnostic() {
            final Context ctx = requireContext().getApplicationContext();
            new Thread(() -> {
                final String report = Diag.INSTANCE.run(ctx);
                if (!isAdded()) return;
                requireActivity().runOnUiThread(() -> showReport(report));
            }, "GintarasTTS-diag").start();
        }

        private void showReport(String text) {
            Context ctx = requireContext();
            final EditText view = new EditText(ctx);
            view.setText(text);
            view.setKeyListener(null);                 // read-only, but selectable/copyable
            view.setTextIsSelectable(true);
            int pad = (int) (16 * ctx.getResources().getDisplayMetrics().density);
            view.setPadding(pad, pad, pad, pad);
            new AlertDialog.Builder(ctx)
                    .setTitle(R.string.cat_diag)
                    .setView(view)
                    .setPositiveButton(R.string.diag_copy, (d, w) -> {
                        ClipboardManager cm = (ClipboardManager) ctx.getSystemService(Context.CLIPBOARD_SERVICE);
                        if (cm != null) cm.setPrimaryClip(ClipData.newPlainText("gintaras-diag", text));
                    })
                    .setNegativeButton(R.string.diag_close, null)
                    .show();
        }
    }
}

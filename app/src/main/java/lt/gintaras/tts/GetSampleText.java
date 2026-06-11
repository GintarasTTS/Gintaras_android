package lt.gintaras.tts;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;

/** Returns the sample text shown in the system TTS settings (GET_SAMPLE_TEXT). */
public class GetSampleText extends Activity {
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent returnData = new Intent();
        returnData.putExtra("sampleText", getString(R.string.sample_text));
        setResult(TextToSpeech.LANG_AVAILABLE, returnData);
        finish();
    }
}

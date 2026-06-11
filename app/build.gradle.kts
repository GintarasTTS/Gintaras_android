plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace   = "lt.gintaras.tts"
    compileSdk  = 34

    defaultConfig {
        applicationId = "lt.gintaras.tts"
        minSdk        = 21
        targetSdk     = 34
        versionCode   = 1
        versionName   = "1.0"

        // Chaquopy: ABIs to bundle Python + numpy for.
        // arm64-v8a covers all modern phones; x86_64 covers the emulator.
        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_1_8
        targetCompatibility = JavaVersion.VERSION_1_8
    }

    kotlinOptions {
        jvmTarget = "1.8"
    }
}

// ---- Chaquopy Python runtime -----------------------------------------------------------
chaquopy {
    defaultConfig {
        version = "3.12"           // Python version bundled in the APK

        pip {
            install("numpy")       // the only non-stdlib dependency of lt_tts
        }

        // lt_tts reads files from its data/ subdirectory via open() / os.path.
        // extractPackages copies the package out of the APK ZIP to the app's
        // private files dir on first run, so Python file I/O works normally.
        extractPackages("lt_tts")
    }

    sourceSets {
        getByName("main") {
            srcDir("src/main/python")   // where lt_tts/ and lt_tts_bridge.py live
        }
    }
}

dependencies {
    // No additional Java/Kotlin dependencies: Chaquopy is applied as a plugin above
    // and injects its runtime AAR automatically.
}

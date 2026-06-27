plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace   = "lt.gintaras.tts"
    compileSdk  = 35

    defaultConfig {
        applicationId = "lt.gintaras.tts"
        minSdk        = 21
        targetSdk     = 35
        // Auto-increment per CI build so every published APK is a newer version than the last
        // (a fixed versionCode means Android never treats a new APK as an update). Falls back to 1
        // for local builds. versionName tracks the same number for a human-readable rolling version.
        versionCode   = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName   = "1.0.${System.getenv("GITHUB_RUN_NUMBER") ?: "0"}"
    }

    signingConfigs {
        // ONE stable key committed to the repo (app/debug.keystore) signs BOTH build types, so a new
        // build installs as an update over the previous one. Previously the CI generated a throwaway
        // keystore on each ephemeral runner -> every release had a different key -> Android rejected
        // updates with INSTALL_FAILED_UPDATE_INCOMPATIBLE. This is the standard throwaway Android debug
        // key (storepass/keypass "android", alias androiddebugkey); it is intentionally public.
        create("release") {
            storeFile     = file("debug.keystore")
            storePassword = "android"
            keyAlias      = "androiddebugkey"
            keyPassword   = "android"
        }
    }

    buildTypes {
        debug {
            signingConfig = signingConfigs.getByName("release")
        }
        release {
            isMinifyEnabled = false
            signingConfig   = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.preference:preference:1.2.1")
    implementation("com.google.android.material:material:1.12.0")
}

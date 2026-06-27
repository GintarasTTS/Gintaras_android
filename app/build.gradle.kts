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
        // Kept at 1 / "1.0" during bug-fixing: with the STABLE signing key below, a same-versionCode
        // APK still reinstalls over the previous one (equal versionCode + matching signature is allowed;
        // only a LOWER versionCode is rejected). Bump these when cutting an actual new release.
        versionCode   = 1
        versionName   = "1.0"
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

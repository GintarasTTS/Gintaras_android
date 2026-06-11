plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace   = "lt.gintaras.tts"
    compileSdk  = 35

    defaultConfig {
        applicationId = "lt.gintaras.tts"
        minSdk        = 21
        targetSdk     = 35
        versionCode   = 1
        versionName   = "1.0"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    signingConfigs {
        // Sign the release APK with the standard Android debug key so the rolling
        // FOSS build is directly installable without a private keystore.
        create("release") {
            storeFile   = file("${System.getProperty("user.home")}/.android/debug.keystore")
            storePassword = "android"
            keyAlias    = "androiddebugkey"
            keyPassword = "android"
        }
    }

    buildTypes {
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

// ---- Chaquopy Python runtime -----------------------------------------------------------
chaquopy {
    defaultConfig {
        version = "3.12"
        pip {
            install("numpy")
        }
        extractPackages("lt_tts")
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.preference:preference:1.2.1")
    implementation("com.google.android.material:material:1.12.0")
}

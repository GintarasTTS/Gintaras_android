plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
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

dependencies {
    // Pure Kotlin engine — no Python runtime needed.
}

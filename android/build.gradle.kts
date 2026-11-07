// Root build file. Plugins are declared here with `apply false` so the version
// catalog resolves them once; :app applies them for real.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.chaquopy) apply false
}

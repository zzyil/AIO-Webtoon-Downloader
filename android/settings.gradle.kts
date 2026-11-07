pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    // Modules must not declare their own repositories; everything resolves from
    // here. Chaquopy publishes to Maven Central (verified: its maven-metadata
    // lives under repo1.maven.org/maven2/com/chaquo/python/gradle/).
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "AIO Downloader"
include(":app")

import org.jetbrains.kotlin.gradle.dsl.JvmTarget
// Imported rather than fully qualified: inside a Kotlin build script `java`
// resolves to the JavaPluginExtension accessor, so `java.time.Instant` fails to
// compile with "Unresolved reference: time".
import java.time.Instant
import java.time.ZoneOffset

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    // Since Kotlin 2.0 the Compose compiler ships WITH the Kotlin plugin and is
    // applied separately. Omitting it fails with "compose compiler plugin not
    // found"; pinning it to a different version than kotlin-android is the
    // classic Compose build break, which is why both use the same version.ref.
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.chaquopy)
}

// ---------------------------------------------------------------------------
// PYTHON SOURCE STAGING
//
// The downloader's Python lives at the REPO ROOT (../..), not inside android/.
// Chaquopy only reads source dirs it owns, so we mirror the shipping subset
// into build/generated/python and point Chaquopy's `main` source set there.
//
// This is the Android analogue of UI-source/scripts/prepare-src.js — when a new
// root-level module appears, BOTH lists need it. grep: ROOT_PYTHON_MODULES.
//
// WHY AN INCLUDE-LIST, NOT AN EXCLUDE-LIST: the repo root also holds one-off
// dev/debug scripts (investigate_comix.py, probe_mangafire_vrf.py,
// calibrate_quality_probe.py, _diag_*.py ...). An exclude-list would ship them
// the moment someone adds another one. An include-list fails closed.
// ---------------------------------------------------------------------------
val repoRoot: File = rootProject.projectDir.parentFile

// ===========================================================================
// APP VERSION — the desktop's date scheme, plus an Android-only versionCode.
//
// `versionName` is byte-identical to what UI-source/scripts/ci-date-version.js
// prints for the same commit: YYYY.MDD.HMM in UTC, off the COMMITTER timestamp.
// Read that script's header before changing anything here — its reasoning about
// leading zeros, prerelease-suffix inversion and committer-vs-build time applies
// to this file unchanged, and is deliberately not restated. The point of sharing
// the formula is that a desktop installer and an APK built from one commit
// report the SAME version string to a human comparing a bug report.
//
// `versionCode` has no desktop analogue and needs its own derivation, because it
// is the only number Android itself compares: PackageInstaller refuses an APK
// whose versionCode is at or below the installed one, so it must be monotonic in
// release order or updates silently stop working.
//
// WHY MINUTES-SINCE-2020 AND NOT THE OBVIOUS YYYYMMDDHHMM:
//   versionCode is a SIGNED 32-BIT int (hard max 2,147,483,647; Google documents
//   2,100,000,000 as the ceiling). 202608201530 overflows that by ~94x. So does
//   the two-digit-year squeeze YYMMDDHHMM — 2,608,201,530 is still over both
//   caps — which is the variant that LOOKS like it fits and does not. Dropping
//   the year entirely (MMDDHHMM) fits, and stops being monotonic every January.
//
//   Minutes elapsed since 2020-01-01T00:00Z is ~3.49e6 today (commit d1ae7d6 ->
//   3489675) and does not reach 2,100,000,000 for another ~3990 years. It is
//   also MINUTE-resolution, exactly like versionName's HMM component, so the two
//   collide under precisely the same condition — two release commits inside one
//   UTC minute, which ci-date-version.js already argues is moot — instead of one
//   of them silently tolerating what the other cannot express.
//
// Consumed by: core/AppUpdater.kt (BuildConfig.VERSION_CODE is what the update
// feed's `versionCode` is compared against) and the `printVersion` task at the
// bottom of this file, which is how .github/workflows/release.yml learns the
// version to name the APK and to write into android-latest.json. Gradle is the
// single source of truth for the Android version the way ci-date-version.js is
// for the desktop's; CI never recomputes the formula in bash.
// ===========================================================================

/** 2020-01-01T00:00:00Z. FROZEN — raising it renumbers every future build DOWN. */
val VERSION_CODE_EPOCH_SECONDS = 1_577_836_800L

/**
 * Committer timestamp of the commit being built, in seconds.
 *
 * Resolution order, first hit wins:
 *   1. `-PaioVersionEpoch=<seconds>` / `AIO_VERSION_EPOCH` — an explicit stamp.
 *      CI does not need it (step 2 works there), but it makes a version reproducible
 *      from a source tarball with no .git, and it is the seam a test would use.
 *   2. `git log -1 --format=%ct` in the repo root. Works in this repo's agent
 *      WORKTREES (where .git is a file) and under actions/checkout's default
 *      depth-1 clone.
 *   3. [FALLBACK_EPOCH_SECONDS], with a loud warning. Reached only when git is
 *      absent — an unpacked source zip. Such a build is never a release.
 */
val FALLBACK_EPOCH_SECONDS = 1_787_217_343L // d1ae7d6, the commit this scheme landed on

fun resolveVersionEpochSeconds(): Long {
    val explicit = (providers.gradleProperty("aioVersionEpoch").orNull
        ?: providers.environmentVariable("AIO_VERSION_EPOCH").orNull)?.trim()
    if (!explicit.isNullOrEmpty()) {
        return explicit.toLongOrNull()
            ?: throw GradleException("aioVersionEpoch is not an integer: '$explicit'")
    }

    // providers.exec rather than ProcessBuilder so this stays legal if
    // org.gradle.configuration-cache is ever flipped on in gradle.properties.
    // runCatching covers the case exec cannot cover: `git` not on PATH at all,
    // which throws at provider-query time regardless of isIgnoreExitValue.
    val fromGit = runCatching {
        val out = providers.exec {
            workingDir = repoRoot
            commandLine("git", "log", "-1", "--format=%ct")
            isIgnoreExitValue = true
        }
        if (out.result.get().exitValue != 0) null
        else out.standardOutput.asText.get().trim().toLongOrNull()
    }.getOrNull()

    if (fromGit != null && fromGit > 0) return fromGit

    logger.warn(
        "aio: no git committer timestamp available (not a git checkout, or git is " +
            "not on PATH) - falling back to the frozen epoch $FALLBACK_EPOCH_SECONDS. " +
            "This build is NOT release-versioned.",
    )
    return FALLBACK_EPOCH_SECONDS
}

val versionEpochSeconds: Long = resolveVersionEpochSeconds()

val aioVersionName: String = run {
    val utc = Instant.ofEpochSecond(versionEpochSeconds).atZone(ZoneOffset.UTC)
    "${utc.year}.${utc.monthValue * 100 + utc.dayOfMonth}.${utc.hour * 100 + utc.minute}"
}

val aioVersionCode: Int = run {
    val minutes = (versionEpochSeconds - VERSION_CODE_EPOCH_SECONDS) / 60
    // Tripwire, not a validator: the arithmetic cannot produce either of these
    // for a real commit. A negative code means someone pointed the build at a
    // pre-2020 commit (Android rejects <= 0); an over-cap one means the epoch
    // constant was edited or a bogus AIO_VERSION_EPOCH was passed in. Both are
    // silent-in-CI failures that only surface as "updates stopped working".
    require(minutes in 1..2_100_000_000L) {
        "versionCode $minutes derived from epoch $versionEpochSeconds is outside " +
            "Android's usable range (1..2100000000). See the APP VERSION block above."
    }
    minutes.toInt()
}

// ===========================================================================
// UPDATE FEED + SIGNING — the two facts core/AppUpdater.kt needs baked in.
//
// FEED: "owner/repo" on GitHub, whose /releases/latest/download/ the updater
// polls. Android's analogue of release.yml's "Point update feed at this repo"
// step, and it follows the same rule: the feed is whichever repo BUILT the APK,
// so a fork-built APK polls the fork. The static default below only ever applies
// to local builds, which never self-update (see RELEASE_SIGNED).
//
// SIGNING: there is no keystore in this repo and there never will be — a private
// key in a public tree is not a key. So `assembleRelease` resolves one of two
// ways:
//   * The four AIO_KEYSTORE_* inputs are present (CI, from repo secrets; see
//     .github/workflows/release.yml) -> sign with the durable release key, and
//     stamp RELEASE_SIGNED = true.
//   * They are absent (every local build, and any CI run on a fork with no
//     secrets) -> sign with the DEBUG key so the APK still installs, and stamp
//     RELEASE_SIGNED = false.
//
// Why debug-signed rather than unsigned: an unsigned APK cannot be installed at
// all (INSTALL_PARSE_FAILED_NO_CERTIFICATES), so "unsigned but honest" is just a
// broken artifact. Why RELEASE_SIGNED must then gate self-update: AGP generates
// ~/.android/debug.keystore per MACHINE, so two CI runs produce two different
// keys, and Android refuses to replace an app with one signed by a different key
// (INSTALL_FAILED_UPDATE_INCOMPATIBLE). A debug-signed build that offered
// updates would download an APK it can provably never install.
// ===========================================================================
val aioUpdateFeedRepo: String = (
    providers.gradleProperty("aioUpdateFeedRepo").orNull
        ?: providers.environmentVariable("AIO_UPDATE_FEED_REPO").orNull
        ?: "zzyil/AIO-Webtoon-Downloader"
    ).trim()

// A malformed value 404s every check forever and reports as "no feed", which is
// indistinguishable from "no release published yet". Fail at configuration time.
require(Regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$").matches(aioUpdateFeedRepo)) {
    "aioUpdateFeedRepo must be 'owner/repo', got '$aioUpdateFeedRepo'"
}

// ===========================================================================
// ABIs — a BUILD INPUT, not a build-type property, and that is not a style
// choice. Chaquopy resolves the ABI set it stages Python for from
// `android.defaultConfig.ndk.abiFilters` UNIONED WITH THE PRODUCT FLAVORS, and
// from nothing else — verified by disassembling PythonPlugin.getAbis in
// gradle-17.0.0.jar, which builds a TreeSet from defaultConfig and then walks
// `variant.productFlavors`. Build types are never consulted.
//
// So the obvious "arm64 in defaultConfig, x86_64 added back in buildTypes.debug"
// SILENTLY PRODUCES A BROKEN APK: AGP honours the build type and packages
// lib/x86_64/*.so, while Chaquopy does not and stages only
// requirements-arm64-v8a.imy + stdlib-arm64-v8a.imy. Measured — the debug APK
// came out 56.6 MB with x86_64 native libs and no x86_64 Python runtime at all,
// which is a crash on the first import on an emulator, not a build error.
//
// One list, one place, overridable per invocation instead:
//   * default BOTH, so a local build and the pre-merge CI job stay runnable on
//     an emulator (android/TESTING.md's recipes assume that).
//   * `-PaioAbis=arm64-v8a` / `AIO_ABIS=arm64-v8a` narrows it. release.yml's
//     `android` job sets the env var at JOB level so assembleRelease and
//     printVersion cannot disagree about what shipped.
//
// WHAT DROPPING x86_64 IS WORTH, MEASURED — because the figure previously
// written here and in android/README.md's ABI row was wrong. It does NOT halve
// the APK: it took the release build from 76.79 MB to 50.24 MB, a 34.6% saving.
// The "half" claim came from counting only lib/*.so (~10.3 MB per ABI) and
// missing that the bulk is assets/chaquopy, where the per-ABI split is real but
// uneven (requirements-x86_64.imy 13.06 MB vs requirements-arm64-v8a.imy
// 10.58 MB) and ~12.5 MB is ABI-agnostic (requirements-common + stdlib-common).
//
// Chaquopy's Python 3.12+ builds are 64-bit ONLY (armeabi-v7a and x86 exist for
// 3.11 and older), so these two are the entire menu.
// ===========================================================================
val aioAbis: List<String> = (
    providers.gradleProperty("aioAbis").orNull
        ?: providers.environmentVariable("AIO_ABIS").orNull
        ?: "arm64-v8a,x86_64"
    ).split(",").map(String::trim).filter(String::isNotEmpty)

require(aioAbis.isNotEmpty() && aioAbis.all { it == "arm64-v8a" || it == "x86_64" }) {
    "aioAbis must be a comma-separated subset of arm64-v8a,x86_64 — got '$aioAbis'. " +
        "Chaquopy publishes no 32-bit runtime for Python 3.12+."
}

val aioReleaseKeystore: File? =
    (providers.gradleProperty("aioKeystoreFile").orNull
        ?: providers.environmentVariable("AIO_KEYSTORE_FILE").orNull)
        ?.trim()
        ?.takeIf { it.isNotEmpty() }
        ?.let { File(it) }
        ?.takeIf { it.isFile }

/**
 * A secret that must exist once a keystore has been supplied. Gradle property
 * first so a developer can test signing without exporting anything; env var is
 * how CI passes repo secrets. Fails at CONFIGURATION time, before three minutes
 * of Chaquopy pip work, and never echoes the value.
 */
fun requireEnv(name: String): String {
    val propertyName = name.lowercase().split('_')
        .mapIndexed { i, part -> if (i == 0) part else part.replaceFirstChar(Char::uppercase) }
        .joinToString("")
    val value = (providers.gradleProperty(propertyName).orNull
        ?: providers.environmentVariable(name).orNull)?.trim()
    require(!value.isNullOrEmpty()) {
        "$name (or -P$propertyName) is required because a release keystore was " +
            "supplied at ${aioReleaseKeystore?.absolutePath}. Supply all four of " +
            "AIO_KEYSTORE_FILE / AIO_KEYSTORE_PASSWORD / AIO_KEY_ALIAS / " +
            "AIO_KEY_PASSWORD, or none of them."
    }
    return value
}

// Deliberately omitted, each for a reason:
//   api.py             - FastAPI REST server; the Android app calls Python in-process.
//   gui.py             - tkinter desktop GUI.
//   metadata_dialog.py - tkinter dialog, pulled in only by gui.py.
//   requirements.txt   - the Android dependency set is the `pip` block below,
//                        not the desktop list (see README "Dependency triage").
val ROOT_PYTHON_MODULES = listOf(
    "aio-dl.py",          // the engine; imported via importlib (the dash is not an identifier)
    "aio_android.py",     // Chaquopy entry point — what Kotlin actually calls
    "aio_search_cli.py",
    "aio_config.py",
    "library_state.py",
    "item_kinds.py",      // chapter/volume naming; imported by library_state + the engine
    "series_ledger.py",   // .aio_series.json namespaces + locked writer
    "metadata_editor.py",
    "metadata_cli.py",
    "migrate_library.py",
)

val stagedPythonDir: File = layout.buildDirectory.dir("generated/python").get().asFile

// Wheels we ship ourselves because no index serves them for Android. See
// android/wheels/README.md for what each one is and how to regenerate it.
val vendoredWheelsDir: File = File(rootProject.projectDir, "wheels")

fun vendoredWheel(name: String): String {
    val f = File(vendoredWheelsDir, name)
    // Same fail-at-configuration-time rule as the Python staging above: a
    // missing wheel here would otherwise surface on device as an ImportError
    // in search, long after the build "succeeded".
    require(f.isFile) { "Vendored wheel not found: $f" }
    return f.absolutePath
}

val stagePythonSources by tasks.registering(Sync::class) {
    description = "Mirror the downloader's Python sources into the Chaquopy source set."
    group = "build"

    ROOT_PYTHON_MODULES.forEach { name ->
        val f = File(repoRoot, name)
        // Fail at configuration time, not with a mystifying ImportError on the
        // device three minutes into a build. `from(missingFile)` is silently a
        // no-op, which is exactly the failure mode worth spending a check on.
        require(f.isFile) { "Expected Python module not found: $f" }
        from(f)
    }

    from(File(repoRoot, "sites")) {
        into("sites")
        // sites/ carries two DATA files besides the handlers —
        // quality_seed.json (read by the comix image-quality probe) and
        // official_publishers.json (metadata layer). A "*.py"-only filter drops
        // them silently and the failure surfaces much later as a bad rating.
        include("**/*.py", "**/*.json")
        exclude("**/__pycache__/**")
    }

    into(stagedPythonDir)
}

android {
    namespace = "com.aio.downloader"
    compileSdk = 36

    defaultConfig {
        // One-line change point if this ever needs a real publisher identity.
        applicationId = "com.aio.downloader"
        minSdk = 26
        targetSdk = 36
        versionCode = aioVersionCode
        versionName = aioVersionName

        ndk {
            abiFilters += aioAbis
        }

        // See the UPDATE FEED + SIGNING block above. Read by core/AppUpdater.kt.
        buildConfigField("String", "UPDATE_FEED_REPO", "\"$aioUpdateFeedRepo\"")
    }

    signingConfigs {
        // Created ONLY when a real keystore was supplied, so its absence is what
        // the release block below keys on. Passwords are required alongside it:
        // a keystore with a wrong/blank password fails deep inside apksigner
        // with a message that reads like file corruption.
        if (aioReleaseKeystore != null) {
            create("release") {
                storeFile = aioReleaseKeystore
                storePassword = requireEnv("AIO_KEYSTORE_PASSWORD")
                keyAlias = requireEnv("AIO_KEY_ALIAS")
                keyPassword = requireEnv("AIO_KEY_PASSWORD")
            }
        }
    }

    buildFeatures {
        compose = true
        // Off by default since AGP 8.0, and required by the three
        // buildConfigField entries above and below. Without it there is no
        // generated BuildConfig for core/AppUpdater.kt to read UPDATE_FEED_REPO
        // and RELEASE_SIGNED from.
        buildConfig = true
    }

    buildTypes {
        release {
            // ── MINIFY STAYS OFF. Restated 2026-08-20, now that the app HAS a
            // UI, rather than left as the M0 placeholder. ──
            //
            // The original reason still holds and is now enumerable. Python
            // reaches back into Kotlin BY NAME through Chaquopy, at three
            // objects R8 sees no Java caller for:
            //   browser/WebViewBridge.kt  - installed via
            //     aio_android.set_browser_bridge; _WebViewBackend calls
            //     goto/evaluate/content/cookies/userAgent/waitForSelector/
            //     solveChallenge on the ProfileBridge it hands back, plus the
            //     @JavascriptInterface `settle` the page itself calls.
            //   core/ImageCodecBridge.kt  - installed via
            //     set_image_codec_bridge; _BridgeImageCodec calls
            //     decodeToPng/encodeWebp/formats.
            // R8 renames all of those, and the failure is a RUNTIME AttributeError
            // inside Python four layers from the cause — mangafire signing
            // returning null tokens, or every WebP page decoding to None. A
            // green build proves nothing about it, and nothing in CI runs on a
            // device, so the keep rules would be validated by hand or not at all.
            //
            // What turning it on would actually buy, MEASURED on the release
            // APK: dex is 11.34 MB of 76.79 MB in-APK (14.8%), against
            // assets/chaquopy at 43.93 MB and lib/ at 10.30 MB. R8 cannot touch
            // either of those — they are Python bytecode and native .so files —
            // so even an implausibly good 50% dex shrink is ~7% off the APK,
            // for a change whose failure mode is invisible until a user's
            // download dies mid-chapter.
            //
            // app/proguard-rules.pro today keeps only the com.chaquo.python
            // runtime — the two bridges above are NOT in it, so flipping this
            // flag alone ships a broken release. Turning it on means: add keeps
            // for those two classes and for @android.webkit.JavascriptInterface
            // members, THEN run the on-device pass that is the only thing that
            // can validate them (install the minified release APK, complete a
            // real download, and exercise both bridges — a mangafire series for
            // the signer, a WebP-page site for the codec). Until someone is
            // willing to do that pass, off is the honest setting.
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )

            // Debug key when no release keystore was supplied — the APK still
            // installs, it just cannot be self-updated. See the SIGNING block.
            signingConfig = signingConfigs.findByName("release")
                ?: signingConfigs.getByName("debug")
            buildConfigField("boolean", "RELEASE_SIGNED", (aioReleaseKeystore != null).toString())
        }

        debug {
            // A debug build is never the artifact a release feed serves, and its
            // signing key is per-machine. Hardcoded rather than derived so the
            // updater's "am I updatable" answer cannot depend on whether the
            // developer happened to have keystore env vars exported.
            buildConfigField("boolean", "RELEASE_SIGNED", "false")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.kotlinx.coroutines.android)
    implementation(libs.coil.compose)
    implementation(libs.coil.network.okhttp)

    // The BOM aligns every compose-* artifact below; none of them carry their
    // own version. platform() is required — a plain implementation() of a BOM
    // silently does nothing.
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons.extended)

    // Debug-only: ui-tooling pulls in the whole preview/inspection runtime and
    // has no business in a release APK.
    debugImplementation(libs.androidx.compose.ui.tooling)

    // core/LogFilter.kt is pure Kotlin with no Android imports precisely so its
    // port of UI-source/electron/log-filter.js can be tested with no device in
    // the loop. `gradlew :app:testDebugUnitTest` runs them.
    testImplementation(libs.junit)
    // The android.jar used for unit tests stubs org.json so every method throws
    // ("not mocked"). core/LibraryModels.kt is ALL JSONObject parsing, and that
    // parsing is where a wrong key silently produces an empty library rather
    // than an error — so the real implementation goes on the test classpath
    // instead of the alternative, which is testing it only on a device.
    testImplementation(libs.json)
}

chaquopy {
    defaultConfig {
        // Matches the desktop interpreter (3.13.x), so behaviour differences
        // between "works on my machine" and "works on the phone" can't come
        // from a Python version gap. Chaquopy auto-detects buildPython on
        // Windows via `py -3.13`; set buildPython(...) here only if that fails.
        version = "3.13"

        // ...which it does on a GitHub Linux runner, where the interpreter that
        // matches `version` above lives in the setup-python toolcache and the
        // bare `python3` on PATH may be the distro's. Both CI workflows export
        // AIO_BUILD_PYTHON from the setup-python step's own `python-path`
        // output; grep AIO_BUILD_PYTHON in .github/workflows/. Unset locally,
        // where auto-detection already works.
        val explicitBuildPython = (providers.gradleProperty("aioBuildPython").orNull
            ?: providers.environmentVariable("AIO_BUILD_PYTHON").orNull)?.trim()
        if (!explicitBuildPython.isNullOrEmpty()) {
            buildPython(explicitBuildPython)
        }

        pip {
            // ---- Where the vendored wheels come from ------------------------
            // android/wheels/ as an extra wheel SOURCE, not as install paths.
            //
            // WHY BY NAME AND NOT BY PATH: Chaquopy runs pip once per ABI, so a
            // literal path would pin ONE ABI's wheel for both. --find-links lets
            // each ABI's pip pick its own file, and pip sorts candidates by BUILD
            // TAG - so the vendored Pillow (build 1) beats the index's build 0
            // while install("Pillow==11.0.0") below stays an ordinary version pin.
            //
            // It also resolves something a version pin cannot: the rebuilt
            // Pillow's METADATA carries `Requires-Dist: chaquopy-libwebp`, a
            // package that exists on NO index. Without this line resolution
            // fails outright rather than falling back.
            //
            // Deleting those wheels is a safe retreat - pip falls back to the
            // index's webp-less Pillow and nothing else changes. Full rationale
            // and the regeneration recipe: android/wheels/README.md.
            options("--find-links", vendoredWheelsDir.absolutePath)

            // ---- Native packages -------------------------------------------
            // PINNED to exactly what Chaquopy's Android wheel index publishes
            // for cp313/arm64-v8a. Unpinned, pip would prefer PyPI's newer
            // releases, find no Android wheel, fall back to the sdist, and fail
            // to compile it. Verify a bump at https://chaquo.com/pypi-13.1/<name>/
            // Resolved from android/wheels/ (build 1, WITH a WebP codec) via the
            // --find-links above; the index's build 0 has no WebP encoder, which
            // is what the Kotlin ImageCodecBridge exists to work around.
            install("Pillow==11.0.0")        // hard dep: aio-dl.py + sites/mangareader.py import at module scope
            install("numpy==1.26.2")         // image-quality scoring (search_orchestrator)
            install("lxml==5.3.0")           // BeautifulSoup's fast parser
            install("cryptography==42.0.8")  // kagane / mangago / mangareader

            // ---- Pure Python, straight from PyPI ---------------------------
            install("requests")
            install("beautifulsoup4")
            install("cloudscraper")
            install("pypdf")

            // ---- Vendored, because no index serves it for Android ----------
            // rapidfuzz gates cross-site search AND AniList enrichment (both
            // lazy-import it, so downloads work without it). Chaquopy's index
            // has no rapidfuzz and PyPI ships no platform-independent wheel,
            // so android/wheels/ carries rapidfuzz's OWN pure-Python build,
            // made from the official sdist via upstream's supported
            // wheel.cmake=false path. Not a substitute matcher — same library,
            // same version, same scorers, and its .py files are byte-identical
            // to a desktop install.
            //
            // A local path rather than install("rapidfuzz==3.14.5"): from an
            // sdist, pip's output depends on whether the BUILD machine happens
            // to have cmake, and with cmake it would produce a host-native
            // extension that has no business in an APK. The vendored file is
            // the same artifact on every machine.
            //
            // The two rapidfuzz backends are not bit-identical; sites/
            // fuzzy_match.py normalizes the difference and
            // tests/test_fuzzy_match.py pins it. Read that header before
            // touching this.
            install(vendoredWheel("rapidfuzz-3.14.5-py3-none-any.whl"))

            // NOT INSTALLED, and why (all degrade through an existing
            // capability flag — see README "Dependency triage"):
            //   curl_cffi / impit  - fast-download fast paths; base.py degrades.
            //   pyvips             - WebP save fast path; falls back to PIL.
            //   patchright / zendriver - desktop browsers, replaced by WebView.
            //   fastapi / uvicorn      - the REST server, unused in-process.
            //   pillow-jxl-plugin      - --modernize only.
            //   torch stack            - opt-in ML rating, ~700 MB.
        }
    }

    sourceSets {
        getByName("main") {
            srcDir(stagedPythonDir)
        }
    }
}

// Chaquopy's per-variant tasks read the staged directory DIRECTLY, and they are
// not downstream of `preBuild` — depending on that anchor alone builds fine by
// luck and then fails Gradle's validation:
//
//   Task ':app:mergeDebugPythonSources' uses this output of task
//   ':app:stagePythonSources' without declaring an explicit [...] dependency.
//
// Matching by name rather than type because the task class (OutputDirTask) is
// internal to the plugin and not on this script's classpath. The filter is
// broad (every Chaquopy task carries "Python" in its name: mergeXPythonSources,
// generateXPythonAssets, generateXPythonRequirements) so a renamed or added
// task stays covered; the self-exclusion keeps it from depending on itself.
//
// This wiring is self-guarding: if a future Chaquopy renames its tasks out of
// this filter, Gradle raises the same validation error above rather than
// silently packaging a stale copy of the Python sources.
tasks.matching { it.name.contains("Python") && it.name != stagePythonSources.name }
    .configureEach { dependsOn(stagePythonSources) }

// ---------------------------------------------------------------------------
// `./gradlew -q :app:printVersion` -> `key=value` lines on stdout.
//
// This exists so .github/workflows/*.yml never reimplements anything this file
// already decides. CI needs the version to name the APK asset and to fill
// android-latest.json, and it needs minSdk/abis for that feed's applicability
// fields — a second implementation of any of them in bash is exactly the drift
// ci-date-version.js was written to prevent on the desktop side. Adding a field
// to the feed means adding a line HERE, never a literal in the workflow.
//
// `abis` is defaultConfig's resolved set, which is the ONLY set — see the ABIs
// block for why it cannot be a per-build-type value.
//
// Read with grep, not `eval`: Gradle can put its own notices on stdout, and the
// workflow must not execute them. The values are captured into locals here
// rather than read inside doLast, because reading project state at execution
// time is what breaks under the configuration cache.
// ---------------------------------------------------------------------------
tasks.register("printVersion") {
    description = "Print the date-derived version and the release APK's applicability facts."
    group = "help"
    val name = aioVersionName
    val code = aioVersionCode
    val signed = aioReleaseKeystore != null
    val feed = aioUpdateFeedRepo
    val minSdkValue = android.defaultConfig.minSdk
    val abis = android.defaultConfig.ndk.abiFilters.sorted().joinToString(",")
    doLast {
        println("versionName=$name")
        println("versionCode=$code")
        println("releaseSigned=$signed")
        println("updateFeedRepo=$feed")
        println("minSdk=$minSdkValue")
        println("abis=$abis")
    }
}

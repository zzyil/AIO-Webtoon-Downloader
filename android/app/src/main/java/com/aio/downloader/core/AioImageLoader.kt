package com.aio.downloader.core

import coil3.ImageLoader
import coil3.PlatformContext
import coil3.network.okhttp.OkHttpNetworkFetcherFactory
import coil3.request.crossfade
import okhttp3.OkHttpClient

/**
 * The app's single Coil [ImageLoader], and the only place remote images get
 * fetched by the UI.
 *
 * WHY THIS EXISTS AT ALL. Coil 3 split network fetching out of `coil-compose`:
 * the core artifact ships fetchers for assets, files, content URIs, byte arrays
 * and resources, and NOTHING that accepts `http`/`https`. The app took only
 * `coil-compose`, so every remote `AsyncImage` silently failed — which was
 * invisible on the Search screen because [ui.screens.SearchScreen] draws a
 * monogram underneath unconditionally, making a dead fetch look identical to a
 * site that supplied no cover. Adding `coil-network-okhttp` restores the
 * scheme; this file supplies the request shape it needs.
 *
 * WHAT THE INTERCEPTOR ADDS, and why it is not a bespoke downloader:
 *  - **Referer** for the hotlink-protected cover CDNs, delegated to
 *    [CoverStore.refererFor] so the host table has exactly ONE definition. The
 *    desktop does the same thing the same way — a header interceptor
 *    (`UI-source/electron/main.js`, grep `pstatic`), not a hand-rolled fetch.
 *  - **User-Agent.** `HttpURLConnection` and OkHttp both default to a
 *    platform-identifying UA (`Dalvik/…`, `okhttp/…`). These two CDNs are picky
 *    enough about request shape to require a Referer at all, so sending a
 *    browser-ish UA costs nothing and removes a plausible silent-failure mode.
 *
 * Installed from [com.aio.downloader.MainActivity] via `SingletonImageLoader`,
 * which every `AsyncImage` without an explicit loader resolves through.
 */
object AioImageLoader {

    /**
     * Chrome on Android, matching what the WebView backend already presents.
     * Not derived from the WebView's real UA on purpose: that requires a
     * WebView instance, and building one to decorate a cover request would drag
     * the browser stack into image loading.
     */
    private const val USER_AGENT =
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) " +
            "Chrome/124.0.0.0 Mobile Safari/537.36"

    private val client: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .addInterceptor { chain ->
                val request = chain.request()
                val builder = request.newBuilder()
                    .header("User-Agent", USER_AGENT)
                CoverStore.refererFor(request.url.host)?.let {
                    builder.header("Referer", it)
                }
                chain.proceed(builder.build())
            }
            .build()
    }

    /**
     * Build the loader. Coil calls this lazily and caches the result, so the
     * OkHttp client is created once per process at first image use rather than
     * at startup.
     *
     * ONE overload only: on Android `coil3.PlatformContext` is a typealias for
     * `android.content.Context`, so a second `create(Context)` convenience is
     * the same signature and fails to compile as a conflicting overload.
     */
    fun create(context: PlatformContext): ImageLoader =
        ImageLoader.Builder(context)
            .components { add(OkHttpNetworkFetcherFactory(callFactory = { client })) }
            .crossfade(true)
            .build()
}

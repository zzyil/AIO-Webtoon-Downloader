# Minification is off (app/build.gradle.kts: isMinifyEnabled = false), so this
# file is currently inert. It exists so turning R8 on later is a one-line change
# with a place to put the rules.
#
# When that day comes, the thing to remember: Chaquopy bridges Java and Python
# REFLECTIVELY, so R8 sees no callers for anything Python touches. At minimum
# the com.chaquo.python runtime and any class passed into Python needs keeping.
-keep class com.chaquo.python.** { *; }

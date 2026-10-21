plugins {
    java
    id("org.jetbrains.intellij.platform") version "2.19.0"
}
group = "io.unimem"
version = "1.0.0"
repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}
dependencies {
    intellijPlatform { intellijIdea("2025.3.3") }
    testImplementation("org.junit.jupiter:junit-jupiter:5.13.4")
}
java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }
tasks.test { useJUnitPlatform() }
intellijPlatform {
    pluginConfiguration {
        name = "UniMem"
        ideaVersion { sinceBuild = "253" }
    }
}

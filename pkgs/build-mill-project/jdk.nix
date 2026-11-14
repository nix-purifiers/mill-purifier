# The JDK that runs Mill's launcher, in the lock run of the mill-purifier
# command (its MILL_PURIFIER_JDK) and in the builds of buildMillProject, and
# Mill's daemon when the build sets `mill-jvm-version: system`. The command
# and the builder take it from here, so a lock and its builds run on the
# same JDK. The file is a package for callPackage.
{ jdk21_headless }: jdk21_headless

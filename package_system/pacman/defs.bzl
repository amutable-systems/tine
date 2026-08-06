"""The public alpm package-system API."""

load("//package_system/pacman:rules.bzl", _pacman_remote_repository = "pacman_remote_repository")

pacman_remote_repository = _pacman_remote_repository

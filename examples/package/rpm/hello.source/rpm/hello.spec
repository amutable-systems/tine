Name: hello
Version: 1
Release: %autorelease
Summary: Exercise dev-mode source-tree RPM package builds
License: MPL-2.0
BuildRequires: gcc
Source1: packaging-marker

%description
An integration fixture for Tine's source-tree RPM package build path.

%build
mkdir -p %{_vpath_builddir}
lto=disabled
%if 0%{?_lto_cflags:1}
lto=enabled
%endif
annobin=disabled
%if 0%{?_annotated_build:1}
annobin=enabled
%endif
printf '%s\n' "$lto" > %{_vpath_builddir}/lto
printf '%s\n' "$annobin" > %{_vpath_builddir}/annobin
%{__cc} %{build_cflags} %{build_ldflags} -o %{_vpath_builddir}/hello hello.c

%install
install -Dpm0755 %{_vpath_builddir}/hello %{buildroot}%{_bindir}/tine-package-hello
install -Dpm0644 %{SOURCE1} %{buildroot}%{_datadir}/tine-package/spec-from-checkout
install -d %{buildroot}%{_datadir}/tine-package
lto="$(cat %{_vpath_builddir}/lto)"
annobin="$(cat %{_vpath_builddir}/annobin)"
touch "%{buildroot}%{_datadir}/tine-package/profile-lto-$lto"
touch "%{buildroot}%{_datadir}/tine-package/profile-annobin-$annobin"

%files
%{_bindir}/tine-package-hello
%{_datadir}/tine-package/*

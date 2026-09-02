Name: hello
Version: 1
Release: %autorelease
Summary: Exercise regular RPM package builds
License: MIT
BuildArch: noarch

%description
An integration fixture for Tine's regular RPM package build path.

%prep

%build

%install
install -d %{buildroot}%{_datadir}/tine-package
touch %{buildroot}%{_datadir}/tine-package/archive

%files
%{_datadir}/tine-package/archive

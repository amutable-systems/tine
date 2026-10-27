// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

// go excludes a file that imports "C" when cgo is disabled, so this only builds with a C compiler.
// The pkg-config directive checks that the build can look a library up with pkg-config.
package main

/*
#cgo pkg-config: zlib
#include <zlib.h>

static int answer(void) { return zlibVersion() != NULL ? 42 : 0; }
*/
import "C"

import "fmt"

func main() {
	fmt.Println(int(C.answer()))
}

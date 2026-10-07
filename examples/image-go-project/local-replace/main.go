// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

package main

import (
	"fmt"

	"example.com/localreplace/greeting"
	"rsc.io/quote"
)

func main() {
	fmt.Println(greeting.Message + ": " + quote.Go())
}

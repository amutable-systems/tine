// SPDX-FileCopyrightText: Amutable GmbH <https://amutable.com/>
// SPDX-License-Identifier: MPL-2.0

package main

import (
	"fmt"

	"example.com/siblings/api"
	"rsc.io/quote"
)

func main() {
	fmt.Println("server using " + api.Message + ": " + quote.Go())
}

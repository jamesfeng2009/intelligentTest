// go_ast：用 go/ast 提取 Go 文件中的函数定义（行号范围）
// 用法: go_ast <file.go>
// 输出: JSON [{name, start_line, end_line}]
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
)

type FuncInfo struct {
	Name      string `json:"name"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, `{"error":"usage: go_ast <file>"}`)
		os.Exit(1)
	}
	fset := token.NewFileSet()
	f, err := parser.ParseFile(fset, os.Args[1], nil, parser.ParseComments)
	if err != nil {
		fmt.Fprintf(os.Stderr, `{"error":"parse: %v"}`, err)
		os.Exit(2)
	}
	var funcs []FuncInfo
	ast.Inspect(f, func(n ast.Node) bool {
		if fn, ok := n.(*ast.FuncDecl); ok {
			funcs = append(funcs, FuncInfo{
				Name:      fn.Name.Name,
				StartLine: fset.Position(fn.Pos()).Line,
				EndLine:   fset.Position(fn.End()).Line,
			})
		}
		return true
	})
	out, _ := json.Marshal(funcs)
	fmt.Println(string(out))
}

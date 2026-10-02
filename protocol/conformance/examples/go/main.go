package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"golang.org/x/crypto/blake2b"
)

// An independent implementation, written from protocol/PROTOCOL.md section 13 alone.

func heldOut(lesson, occasion, salt string, rate float64) bool {
	if rate <= 0 {
		return false
	}
	if rate >= 1 {
		return true
	}
	h, _ := blake2b.New(8, nil)
	h.Write([]byte(strings.Join([]string{salt, lesson, occasion}, "\x1f")))
	u := float64(binary.BigEndian.Uint64(h.Sum(nil))) / 18446744073709551616.0
	return u < rate
}

func sha(s string) string { sum := sha256.Sum256([]byte(s)); return hex.EncodeToString(sum[:]) }

func main() {
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<24)
	genesis := sha("commontrace-value-ledger-v1")
	for sc.Scan() {
		var req map[string]interface{}
		json.Unmarshal(sc.Bytes(), &req)
		switch req["op"] {
		case "assign":
			fmt.Printf("{\"held_out\": %v}\n", heldOut(req["lesson"].(string), req["occasion"].(string), req["salt"].(string), req["rate"].(float64)))
		case "ledger":
			prev := genesis
			hashes := []string{}
			for i, r := range req["rows"].([]interface{}) {
				m := r.(map[string]interface{})
				oi, rate := m["occasions_improved"].(float64), m["rate"].(float64)
				row := strings.Join([]string{fmt.Sprint(i), m["slug"].(string), m["verdict"].(string),
					fmt.Sprintf("%.6f", oi), fmt.Sprintf("%.6f", rate), fmt.Sprintf("%.6f", oi*rate)}, "\x1f")
				prev = sha(prev + "\x1f" + row)
				hashes = append(hashes, prev)
			}
			out, _ := json.Marshal(map[string]interface{}{"hashes": hashes})
			fmt.Println(string(out))
		case "digest":
			rows := []string{}
			for _, r := range req["rows"].([]interface{}) {
				cells := []string{}
				for _, c := range r.([]interface{}) {
					cells = append(cells, c.(string))
				}
				rows = append(rows, strings.Join(cells, "\x1f"))
			}
			sort.Strings(rows)
			fmt.Printf("{\"digest\": \"%s\"}\n", sha("commontrace-raw-assignments-v1\x1e"+strings.Join(rows, "\x1e")))
		default:
			fmt.Println("{\"error\": \"unsupported\"}")
		}
	}
}

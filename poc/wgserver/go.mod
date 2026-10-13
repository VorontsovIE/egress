module wgserver

go 1.23

require (
	github.com/metacubex/sing v0.5.8
	github.com/metacubex/sing-wireguard v0.0.0-20260826105301-c3ae17d19f9e
	github.com/metacubex/wireguard-go v0.0.0-20250820062549-a6cecdd7f57f
)

require (
	github.com/google/btree v1.1.3 // indirect
	github.com/metacubex/gvisor v0.0.0-20260826100401-79317d808312 // indirect
	golang.org/x/crypto v0.33.0 // indirect
	golang.org/x/net v0.35.0 // indirect
	golang.org/x/sys v0.30.0 // indirect
	golang.org/x/time v0.10.0 // indirect
)

replace github.com/metacubex/wireguard-go => ./third_party/wireguard-go

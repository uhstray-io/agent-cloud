ui = true

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

storage "raft" {
  path    = "/openbao/data"
  node_id = "node1"
}

api_addr     = "http://0.0.0.0:8200"
cluster_addr = "http://127.0.0.1:8201"

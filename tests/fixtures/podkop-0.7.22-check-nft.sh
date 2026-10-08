# Diagnostic fixture from Podkop 0.7.22 (GPL-2.0; see PODKOP-LICENSE).
# https://github.com/itdoginfo/podkop/blob/0.7.22/podkop/files/usr/bin/podkop
# Commit c0a2736bb95884c19fedf638345ed6148c5fd6af
check_nft_rules() {
    local table_exist=0
    local rules_mangle_exist=0
    local rules_mangle_counters=0
    local rules_mangle_output_exist=0
    local rules_mangle_output_counters=0
    local rules_proxy_exist=0
    local rules_proxy_counters=0
    local rules_other_mark_exist=0

    # Generate traffic through PodkopTable
    curl -m 3 -s "https://$CHECK_PROXY_IP_DOMAIN/check" > /dev/null 2>&1 &
    local pid1=$!
    curl -m 3 -s "https://$FAKEIP_TEST_DOMAIN/check" > /dev/null 2>&1 &
    local pid2=$!

    wait $pid1 2> /dev/null
    wait $pid2 2> /dev/null
    sleep 1

    # Check if PodkopTable exists
    if nft list table inet "$NFT_TABLE_NAME" > /dev/null 2>&1; then
        table_exist=1

        # Check mangle chain rules
        if nft list chain inet "$NFT_TABLE_NAME" mangle > /dev/null 2>&1; then
            local mangle_output
            mangle_output=$(nft list chain inet "$NFT_TABLE_NAME" mangle)
            if echo "$mangle_output" | grep -q "counter"; then
                rules_mangle_exist=1

                if echo "$mangle_output" | grep "counter" | grep -qv "packets 0 bytes 0"; then
                    rules_mangle_counters=1
                fi
            fi
        fi

        # Check mangle_output chain rules
        if nft list chain inet "$NFT_TABLE_NAME" mangle_output > /dev/null 2>&1; then
            local mangle_output_output
            mangle_output_output=$(nft list chain inet "$NFT_TABLE_NAME" mangle_output)
            if echo "$mangle_output_output" | grep -q "counter"; then
                rules_mangle_output_exist=1

                if echo "$mangle_output_output" | grep "counter" | grep -qv "packets 0 bytes 0"; then
                    rules_mangle_output_counters=1
                fi
            fi
        fi

        # Check proxy chain rules
        if nft list chain inet "$NFT_TABLE_NAME" proxy > /dev/null 2>&1; then
            local proxy_output
            proxy_output=$(nft list chain inet "$NFT_TABLE_NAME" proxy)
            if echo "$proxy_output" | grep -q "counter"; then
                rules_proxy_exist=1

                if echo "$proxy_output" | grep "counter" | grep -qv "packets 0 bytes 0"; then
                    rules_proxy_counters=1
                fi
            fi
        fi
    fi

    # Check for other mark rules outside PodkopTable
    nft list tables 2> /dev/null | while read -r _ family table_name; do
        [ -z "$table_name" ] && continue

        [ "$table_name" = "$NFT_TABLE_NAME" ] && continue

        if nft list table "$family" "$table_name" 2> /dev/null | grep -q "meta mark set"; then
            touch /tmp/podkop_mark_check.$$
            break
        fi
    done

    if [ -f /tmp/podkop_mark_check.$$ ]; then
        rules_other_mark_exist=1
        rm -f /tmp/podkop_mark_check.$$
    fi

    echo "{\"table_exist\":$table_exist,\"rules_mangle_exist\":$rules_mangle_exist,\"rules_mangle_counters\":$rules_mangle_counters,\"rules_mangle_output_exist\":$rules_mangle_output_exist,\"rules_mangle_output_counters\":$rules_mangle_output_counters,\"rules_proxy_exist\":$rules_proxy_exist,\"rules_proxy_counters\":$rules_proxy_counters,\"rules_other_mark_exist\":$rules_other_mark_exist}" | jq .
}

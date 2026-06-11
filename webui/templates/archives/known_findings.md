| 总结 | 漏洞类型 | 安全评分 | 源文件 |
| --- | --- | --- | --- |
| 在默认浅层校验下，RESTORE 接受了损坏的哈希 listpack，随后 HGETALL 在 lpAssertValidEntry 中中止崩溃。 | crash | medium | src/rdb.c#L3254::rdbLoadObject. src/listpack.c#L1697::lpAssertValidEntry |

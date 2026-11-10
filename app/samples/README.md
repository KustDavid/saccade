# Sample PDFs for the tests

The end-to-end tests convert real papers. PDFs are not kept in the repository, so fetch the two
public arXiv samples yourself (tests that need a missing file are skipped):

```sh
curl -L -o 1706.03762.pdf https://arxiv.org/pdf/1706.03762   # Attention Is All You Need
curl -L -o 1512.03385.pdf https://arxiv.org/pdf/1512.03385   # Deep Residual Learning
```

`jf-malmendier-2026.pdf` (a Journal of Finance article) is used by one more test if you have your own copy.

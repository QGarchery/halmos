// SPDX-License-Identifier: AGPL-3.0
pragma solidity >=0.8.0 <0.9.0;

contract WeightsTest {
    int256[3] v;
    uint256[3] r;

    function testBalance(uint256 left, uint256 right, uint256 r0, uint256 r1, uint256 r2) public view {
        require(r[0] == r0);
        require(r[1] == r1);
        require(r[2] == r2);
        (bool seenNonZero, uint256 i) = oneNonZero();
        require(seenNonZero);
        uint256 b = balance(left, right);
        assert(i != r[b]);
    }

    function balance(uint256 left, uint256 right) public view returns (uint256) {
        require(left < 3 && right < 3);
        if (v[left] < v[right]) return 0;
        if (v[left] > v[right]) return 1;
        return 2;
    }

    function oneNonZero() public view returns (bool seenNonZero, uint256 index) {
        seenNonZero = false;

        for (uint256 k; k < 3; k++) {
            if (v[k] == 0) continue;
            if (seenNonZero) return (true, k);
            seenNonZero = true;
            index = k;
        }
    }
}

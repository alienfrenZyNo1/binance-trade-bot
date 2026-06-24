const binanceApi = {
  // ... existing code ...
  async getPositions() {
    const response = await this.http.get('/fapi/v1/positionRisk');
    const positions = response.data;
    positions.forEach(position => {
      if (position.marginType === 'cross') {
        // Update position margin type to CROSS in the UI and documentation
        position.marginType = 'CROSS';
      }
    });
    return positions;
  }
};

module.exports = binanceApi;
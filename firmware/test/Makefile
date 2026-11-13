CXX ?= g++
CXXFLAGS ?= -std=c++17 -Wall -Wextra -Werror -O1 -g

SRC = test_main.cpp ../GeoCollar/CaiXianLin.cpp ../GeoCollar/Fence.cpp ../GeoCollar/Protocol.cpp

test: geocollar_tests
	./geocollar_tests

geocollar_tests: $(SRC) ../GeoCollar/*.h
	$(CXX) $(CXXFLAGS) -o $@ $(SRC)

clean:
	rm -f geocollar_tests

.PHONY: test clean
